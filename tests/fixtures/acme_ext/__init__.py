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

import pathlib
from typing import TYPE_CHECKING

from varda.ext import Extension, Finding, RuleSet

if TYPE_CHECKING:
    from collections.abc import Iterator

    from varda.ext import Model

HERE = pathlib.Path(__file__).parent

RULES = RuleSet(tag="ACME")


@RULES.rule("ACME101", "error", "Every table names a cost center")
def acme101(model: Model) -> Iterator[Finding]:
    for table in model.tables:
        # Acme reads its own annotations, and only those, by its prefix.
        if not table.annotations.of("acme").get("cost_center"):
            yield Finding(
                table,
                "no acme:cost_center; storage has to be billed to someone",
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


EXTENSION = Extension(
    name="acme",
    prefix="acme",
    version="1.2.0",
    profile=HERE / "acme.yaml",
    rules=RULES,
    package="acme_ext",
    severity_defaults={"V705": "error"},
)
