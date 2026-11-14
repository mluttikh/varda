"""An extension, as a third party would write one.

This is the fixture the test suite uses, and it is deliberately written the
way a real organization would write one: a profile declaring the vocabulary,
a rule set under its own tag, and one `Extension` exported as `EXTENSION`.

Note what it does *not* do. Everything it runs on comes from `varda.ext`,
the types its rules are annotated with included; it never adds a value to a
Varda enum; and its rule codes all begin with its own tag. Those three
constraints are what the registry checks, and this fixture exists partly to
prove they are satisfiable.
"""

from __future__ import annotations

import itertools
import pathlib
from typing import TYPE_CHECKING

from varda.ext import Determines, Extension, Finding, RuleSet

if TYPE_CHECKING:
    from collections.abc import Iterator

    from varda.ext import Claim, Model

HERE = pathlib.Path(__file__).parent

RULES = RuleSet(tag="ACME")


@RULES.rule("ACME101", "error", "Every fact says how long its rows are kept")
def acme101(model: Model) -> Iterator[Finding]:
    """Flag a fact with no retention period.

    A requirement the profile cannot state: `required: true` means every
    table, and a dimension's rows are kept as long as a fact points at
    them. That every table names a cost center *is* declared, in
    `acme.yaml`, and Varda's V006 enforces it with no rule here.
    """
    for table in model.facts:
        # Acme reads its own annotations, and only those, by its prefix.
        if table.annotations.of("acme").get("retention_days") is None:
            yield Finding(
                table,
                "no acme:retention_days; a fact grows until somebody says "
                "when its rows go",
            )


@RULES.rule("ACME102", "warning", "Restricted columns are not keys")
def acme102(model: Model) -> Iterator[Finding]:
    """Flag a restricted column used as a key.

    A key propagates: it is copied into every fact that references the
    dimension, so classifying it restricted in one place and then joining on
    it everywhere is a control that does not hold.
    """
    for table in model.tables:
        for column in table.columns:
            if column.annotations.of("acme").get("sensitivity") != "restricted":
                continue
            if not column.is_key:
                continue
            yield Finding(
                column,
                "restricted column used as a key; it will be copied into "
                "every table that references this one",
            )


def claims(model: Model) -> Iterator[Claim]:
    """Hold a dimension marked `acme:strict_hierarchies` to strict roll-up.

    Varda reads a plain level's column as naming a member only under its
    parent — several states have a Springfield — and so claims nothing
    about it. Acme's dimensions say otherwise when they are marked, and
    this is where it says so: each level's column determines the column of
    the level above, checked by Varda's expectations beside its own.
    """
    for table in model.dimensions:
        marked = table.annotations.of("acme").get("strict_hierarchies")
        if str(marked).lower() != "true":
            continue
        for hierarchy in table.hierarchies:
            levels = hierarchy.levels
            for parent, child in itertools.pairwise(levels):
                if parent.is_reference or child.is_reference:
                    continue  # Varda claims these already
                if parent.column is None or child.column is None:
                    continue  # V601 says why
                yield Determines(
                    hierarchy,
                    f"each {child.spec} is in one {parent.spec}",
                    (child.column,),
                    (parent.column,),
                )


EXTENSION = Extension(
    name="acme",
    prefix="acme",
    version="1.2.0",
    profile=HERE / "acme.yaml",
    rules=RULES,
    package="acme_ext",
    severity_defaults={"V705": "error"},
    claims=claims,
)
