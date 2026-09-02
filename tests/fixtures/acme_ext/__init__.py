"""An extension, as a third party would write one.

This is the fixture the test suite uses, and it is deliberately written the
way a real organization would write one: a profile declaring the vocabulary,
a rule set under its own tag, and one `Extension` exported as `EXTENSION`.

Note what it does *not* do. Everything it runs on comes from `varda.ext`, it
never adds a value to a Varda enum, and its rule codes all begin with its own
tag. Those three constraints are what the registry checks, and this fixture
exists partly to prove they are satisfiable — it did not, for three releases,
because `Finding` and `RuleSet` lived in `varda.rules` and this file said
otherwise twelve lines below the claim.

The one remaining import is `DimensionalModel`, and it is type-only. A rule's
signature names the model — `varda.ext` names it too, in `Context.model` and
in the type of a rule function — so an extension writing a typed rule cannot
avoid it. Whether that whole read API is public and versioned or internal and
free to move is still undecided; until it is, this import is the honest
statement of what an extension depends on.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from varda.ext import Extension, Finding, RuleSet

if TYPE_CHECKING:
    from collections.abc import Iterator

    from varda.model import DimensionalModel

HERE = pathlib.Path(__file__).parent

RULES = RuleSet(tag="ACME")

#: Acme reads its own annotations through a reader bound to its own prefix.
A = Extension.reader("acme")


@RULES.rule("ACME101", "error", "Every table names a cost center")
def acme101(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        if not A.get(table.cls, "cost_center"):
            yield Finding(
                "ACME101",
                "error",
                str(table),
                "no acme:cost_center; storage has to be billed to someone",
            )


@RULES.rule("ACME102", "warning", "Restricted columns are not keys")
def acme102(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a restricted column used as a key.

    A key propagates: it is copied into every fact that references the
    dimension, so classifying it restricted in one place and then joining on
    it everywhere is a control that does not hold.
    """
    for table in model.tables:
        for column in table.columns:
            if A.get(column.slot, "sensitivity") != "restricted":
                continue
            if not column.is_key:
                continue
            yield Finding(
                "ACME102",
                "warning",
                str(column),
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
