"""The test suite.

Structured around what could actually go wrong rather than around the module
layout: a section per property the design depends on. The largest section is
extension validation, because those are the failures that are unfixable once
somebody has shipped a model against them.

That arrangement is worth keeping and stopped being visible. At four thousand
lines the sections are the index, so they are listed here — a banner rule is
one of these, and `# --- name ---` is a subsection of the banner above it,
never a section in its own right.

    Helpers
    The model layer
    Rules — one per rule, named for the failure it catches
    The rule set itself
    The registry and the extension mechanism
    Extension validation — unfixable once somebody has shipped against it
    Generation
    The command line
    varda.toml — the extension route that needs no Python at all
    Distribution — the routes a shipped extension takes
    The namespace — the one identifier that can never change
    Versioning columns
    Hierarchies — the named paths a dimension is drilled down
    Bridges, and the annotations that carry structure
    Identity — what makes two rows the same thing
    Physical names — the collisions no rule used to see
    Dialects — the spellings the one model comes out in
    Types — what a column says it holds
    Constraints — how much the database is asked to police
    Interop — the claim that a Varda model is an ordinary LinkML schema
    SQLAlchemy — the same model as objects, checked against the DDL
    Portability — what only breaks on somebody else's machine
    The public boundary — what an extension is entitled to import
    Rules that raise — somebody else's code failing inside this one
    Imports — when a model is more than one file
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import importlib
import importlib.metadata
import json
import pathlib
import shutil
import subprocess
import textwrap
import warnings
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
import sqlalchemy as sa
import sqlglot
import yaml
from linkml_runtime.utils.schemaview import SchemaView
from sqlalchemy.dialects import mssql as sa_mssql
from sqlalchemy.dialects import postgresql as sa_postgresql
from sqlalchemy.schema import CreateTable
from sqlglot import expressions as sqlglot_exp

import varda
from varda import (
    __version__,
    cli,
    gen_sql,
    gen_sqlalchemy,
    registry,
    rules,
)
from varda import ext as ext_module
from varda.ext import (
    Context,
    Extension,
    ExtensionError,
    Generator,
    RuleError,
)
from varda.gen_assertions import generate as generate_assertions
from varda.gen_docs import generate as generate_docs
from varda.gen_sql import GenerationError
from varda.gen_sql import generate as generate_sql
from varda.model import PROFILE, TYPES, DimensionalModel, physical_name
from varda.rules import RuleSet

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = pathlib.Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"
RETAIL = EXAMPLES / "retail.yaml"
SNOWFLAKE = EXAMPLES / "snowflake.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build(
    tmp_path: pathlib.Path,
    classes: dict[str, Any],
    imports: list[str] | None = None,
) -> DimensionalModel:
    """Write a minimal LinkML schema and load it as a model.

    Tests state only the classes they care about. Everything a rule needs to
    be *provoked* is in the class; everything else is boilerplate that would
    otherwise be repeated thirty times and read past.

    Loaded through the import map, the way the CLI loads one, so a test can
    write ``imports=["varda"]`` and resolve the installed profile.
    """
    schema = {
        "id": "https://example.org/t",
        "name": "t",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "varda": "https://w3id.org/varda/",
        },
        "default_prefix": "t",
        "default_range": "string",
        "imports": ["linkml:types", *(imports or [])],
        "classes": classes,
    }
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(schema), encoding="utf-8")
    return DimensionalModel.load(path, importmap=registry.importmap())


def codes(model: DimensionalModel) -> set[str]:
    """Return the rule codes that fired against a model."""
    return {f.rule for f in rules.check(model)}


def dimension(**extra: Any) -> dict[str, Any]:
    """Build a minimal legal dimension, for tests that need one to point at."""
    base: dict[str, Any] = {
        "annotations": {"varda:role": "DIMENSION", "varda:scd": "TYPE_1"},
        "attributes": {
            "d_key": {
                "range": "integer",
                "annotations": {"varda:role": "SURROGATE_KEY"},
            },
            # Required, because a dimension's only identity being absent is
            # what V307 is about and every test would otherwise carry it.
            "d_id": {
                "required": True,
                "annotations": {"varda:role": "NATURAL_KEY"},
            },
        },
    }
    base.update(extra)
    return base


def versioned(**columns: Any) -> dict[str, Any]:
    """Build a type-2 dimension carrying the given versioning columns."""
    base = dimension()
    base["annotations"] = {"varda:role": "DIMENSION", "varda:scd": "TYPE_2"}
    base["attributes"] = {**base["attributes"], **columns}
    return base


def _col(role: str, rng: str = "datetime") -> dict[str, Any]:
    return {"range": rng, "annotations": {"varda:role": role}}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Reset the registry between tests.

    Every lookup in the registry is cached, and the cache is keyed on nothing
    — it is valid only for one extension set. A test that injects an
    extension would otherwise leak its vocabulary into every test after it.
    """
    registry.reset_caches()
    yield
    registry.reset_caches()


# ---------------------------------------------------------------------------
# The model layer
# ---------------------------------------------------------------------------


def test_example_loads() -> None:
    model = DimensionalModel.load(RETAIL)
    assert len(model.tables) == 8
    assert len(model.facts) == 2
    assert len(model.dimensions) == 5
    assert len(model.bridges) == 1


@pytest.mark.parametrize("path", [RETAIL, SNOWFLAKE], ids=lambda p: p.stem)
def test_every_example_is_clean(path: pathlib.Path) -> None:
    """A shipped example must pass its own rules.

    An example that does not conform is worse than no example: it is the
    first thing anybody copies.
    """
    assert rules.check(DimensionalModel.load(path)) == []


@pytest.mark.parametrize("path", [RETAIL, SNOWFLAKE], ids=lambda p: p.stem)
def test_every_example_generates_runnable_ddl(path: pathlib.Path) -> None:
    """And its DDL must execute, which is a separate claim from conforming.

    `snowflake.yaml` is the one that would have caught the ordering bug:
    its dimensions reference each other, so by-name creation order emits a
    foreign key before its target exists.
    """
    executes(generate_sql(DimensionalModel.load(path)))


def test_the_snowflake_example_uses_the_forms_it_exists_for() -> None:
    """It is there to be the runnable version of what Concepts describes.

    Asserted rather than assumed: an example added to cover four forms is
    one edit away from covering three, and nothing else would notice.
    """
    model = DimensionalModel.load(SNOWFLAKE)
    levels = [
        lv
        for table in model.dimensions
        for hierarchy in table.hierarchies
        for lv in hierarchy.resolved
    ]
    assert any(lv.is_reference for lv in levels), "no reference level"
    assert any(lv.declared_key for lv in levels), "no declared level key"
    assert any(len(t.unique_keys) > 1 for t in model.dimensions), (
        "no dimension identified two ways"
    )
    assert any(
        c.references and (m := model.table(c.references)) and m.is_dimension
        for t in model.dimensions
        for c in t.foreign_keys
    ), "no dimension references another dimension"


@pytest.mark.parametrize(
    ("logical", "expected"),
    [
        ("DimCustomer", "dim_customer"),
        ("FctSale", "fct_sale"),
        ("already_snake", "already_snake"),
        ("HTTPRequest", "httprequest"),
        ("Dim2Store", "dim2_store"),
    ],
)
def test_physical_name(logical: str, expected: str) -> None:
    assert physical_name(logical) == expected


def test_physical_name_override(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "DimThing": dimension(
                annotations={
                    "varda:role": "DIMENSION",
                    "varda:physical_name": "d_thing_v2",
                }
            )
        },
    )
    assert model.tables[0].physical == "d_thing_v2"


def test_columns_include_inherited(tmp_path: pathlib.Path) -> None:
    """A table inheriting columns sees them.

    Read through `class_induced_slots` rather than `attributes`, because a
    validator that does not understand inheritance disagrees with a model
    that uses it, and nobody can debug that.
    """
    model = build(
        tmp_path,
        {
            "Base": {
                "attributes": {
                    "audit_ts": {
                        "range": "datetime",
                        "annotations": {"varda:role": "ATTRIBUTE"},
                    }
                }
            },
            "DimThing": {**dimension(), "is_a": "Base"},
        },
    )
    table = model.table("DimThing")
    assert table is not None
    assert "audit_ts" in {c.name for c in table.columns}


def test_a_model_may_import_the_profile(tmp_path: pathlib.Path) -> None:
    """`imports: - varda` resolves and changes nothing about the model.

    The reason `varda importmap` exists. A profile is an ordinary LinkML
    schema, so importing it brings its classes into the model's view — and
    the classes declaring the vocabulary must not become tables. Checked
    end to end rather than against `is_model_object` alone, because the
    symptom was sixteen findings, not one predicate.
    """
    model = build(tmp_path, {"DimThing": dimension()}, imports=["varda"])
    assert [t.name for t in model.tables] == ["DimThing"]
    assert codes(model) == set()


def test_a_class_declaring_vocabulary_is_not_a_table(
    tmp_path: pathlib.Path,
) -> None:
    """Flag a class that declares annotations rather than using them.

    Matched across prefixes, so an extension's own declaration class is
    excluded on the same terms as Varda's — an extension that had to be
    special-cased here would not be using the mechanism the core uses.
    """
    model = build(
        tmp_path,
        {
            "DimThing": dimension(),
            "MyTableAnnotations": {
                "annotations": {"varda:applies_to": "table"},
                "attributes": {"whatever": {"range": "string"}},
            },
            "AcmeTableAnnotations": {
                "annotations": {"acme:applies_to": "table"},
                "attributes": {"cost_center": {"range": "string"}},
            },
        },
    )
    assert [t.name for t in model.tables] == ["DimThing"]


# ---------------------------------------------------------------------------
# Rules — one test per rule, naming the failure it is meant to catch
# ---------------------------------------------------------------------------


def test_v001_unknown_annotation(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "DimThing": dimension(
                annotations={"varda:role": "DIMENSION", "varda:grian": "x"}
            )
        },
    )
    assert "V001" in codes(model)


def test_v001_message_names_the_profile(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "DimThing": dimension(
                annotations={"varda:role": "DIMENSION", "varda:nope": "x"}
            )
        },
    )
    found = [f for f in rules.check(model) if f.rule == "V001"]
    assert "varda.yaml" in found[0].message


def test_v002_bad_enum_value(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {"DimThing": dimension(annotations={"varda:role": "dimenson"})},
    )
    found = [f for f in rules.check(model) if f.rule == "V002"]
    assert found
    assert "TableRole" in found[0].message


def test_v003_unknown_prefix(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "DimThing": dimension(
                annotations={"varda:role": "DIMENSION", "nope:thing": "x"}
            )
        },
    )
    found = [f for f in rules.check(model) if f.rule == "V003"]
    assert found
    assert found[0].severity == "warning"


def test_v003_is_not_an_error() -> None:
    """A model annotated by an uninstalled extension is still portable.

    Erroring here would mean a model could only be read on machines with
    every contributing extension installed, which defeats the purpose of
    annotations being inert to tools that do not understand them.
    """
    _, severity, _, _ = next(r for r in rules.all_rules() if r[0] == "V003")
    assert severity == "warning"


def _instantiating(
    tmp_path: pathlib.Path, table: str | None = None, column: str | None = None
) -> DimensionalModel:
    """Build a dimension declaring the given `instantiates`, if any."""
    cls = dimension()
    if table is not None:
        cls["instantiates"] = [table]
    if column is not None:
        cls["attributes"]["d_key"]["instantiates"] = [column]
    return build(tmp_path, {"DimThing": cls})


def test_v005_the_right_metaclass_passes(tmp_path: pathlib.Path) -> None:
    """The declaration Varda's own examples carry.

    `instantiates` is LinkML's metaslot for exactly the relationship the
    profile already describes from the other side with `varda:applies_to`:
    a class whose attributes are the annotation keys legal on an element.
    Varda built that mechanism before declaring it; this is the declaration.
    """
    model = _instantiating(
        tmp_path, "varda:TableAnnotations", "varda:ColumnAnnotations"
    )
    assert "V005" not in codes(model)


def test_v005_is_optional(tmp_path: pathlib.Path) -> None:
    """Silence is not a finding.

    Every annotated column is governed by the same class, so the
    declaration restates what the annotations imply and Varda needs none of
    it. Requiring it would put a constant on forty-two columns of
    `retail.yaml` to say something the next line already says.
    """
    assert "V005" not in codes(_instantiating(tmp_path))


def test_v005_a_metaclass_for_the_other_kind(tmp_path: pathlib.Path) -> None:
    """The failure worth catching: a declaration that reads correctly.

    `varda:ColumnAnnotations` on a class is a real name, spelled right, in
    an active profile — everything except true. Nothing else in the file
    would report it.
    """
    model = _instantiating(
        tmp_path, "varda:ColumnAnnotations", "varda:TableAnnotations"
    )
    found = [f for f in rules.check(model) if f.rule == "V005"]
    assert len(found) == 2
    assert "governs a column and this is a table" in found[0].message


def test_v005_an_unknown_metaclass(tmp_path: pathlib.Path) -> None:
    model = _instantiating(tmp_path, "varda:NoSuchThing")
    found = [f for f in rules.check(model) if f.rule == "V005"]
    assert len(found) == 1
    assert "no active profile declares" in found[0].message


def test_v005_a_bare_name_names_nothing(tmp_path: pathlib.Path) -> None:
    """An annotation class belongs to the extension that declares it.

    A bare `TableAnnotations` says nothing about whose, and resolving it
    against the union of everybody's vocabulary is the collision V001
    already refuses to make.
    """
    found = [
        f
        for f in rules.check(_instantiating(tmp_path, "TableAnnotations"))
        if f.rule == "V005"
    ]
    assert len(found) == 1


def test_governs_is_the_inverse_of_applies_to() -> None:
    """One relationship, read from both ends, against the shipped profile."""
    assert registry.governs("varda:TableAnnotations") == "table"
    assert registry.governs("varda:ColumnAnnotations") == "column"
    assert registry.governs("varda:Hierarchy") is None
    assert registry.governs("TableAnnotations") is None


def test_v101_table_without_role(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "Thing": {
                "annotations": {
                    "varda:grain_statement": "one row per thing here"
                }
            }
        },
    )
    assert "V101" in codes(model)


def test_v102_column_without_role(tmp_path: pathlib.Path) -> None:
    table = dimension()
    table["attributes"]["stray"] = {"range": "string"}
    model = build(tmp_path, {"DimThing": table})
    assert "V102" in codes(model)


def _fact(**annotations: Any) -> dict[str, Any]:
    """Build a fact with one foreign key and one degenerate dimension.

    Enough structure for a legal grain to be built out of, so grain tests can
    vary only the annotation under test.
    """
    return {
        "annotations": {"varda:role": "FACT", **annotations},
        "attributes": {
            "d_key": {
                "range": "integer",
                "annotations": {
                    "varda:role": "FOREIGN_KEY",
                    "varda:references": "DimThing",
                },
            },
            "ticket": {"annotations": {"varda:role": "DEGENERATE_DIMENSION"}},
            "amount": {
                "range": "decimal",
                "unit": {"symbol": "EUR"},
                "annotations": {
                    "varda:role": "MEASURE",
                    "varda:additivity": "ADDITIVE",
                },
            },
        },
    }


def test_v103_fact_without_grain(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": _fact(**{"varda:grain_statement": "one row per ticket"}),
            "DimThing": dimension(),
        },
    )
    assert "V201" in codes(model)


def test_v104_fact_without_grain_statement(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {"FctX": _fact(**{"varda:grain": ["d_key"]}), "DimThing": dimension()},
    )
    assert "V202" in codes(model)


def test_v104_grain_statement_is_a_phrase(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": _fact(
                **{"varda:grain": ["d_key"], "varda:grain_statement": "daily"}
            ),
            "DimThing": dimension(),
        },
    )
    assert "V202" in codes(model)


def test_v114_grain_names_a_missing_column(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": _fact(
                **{
                    "varda:grain": ["d_key", "nonesuch"],
                    "varda:grain_statement": "one row per thing per other",
                }
            ),
            "DimThing": dimension(),
        },
    )
    assert "V203" in codes(model)


def test_v115_grain_cannot_be_built_from_a_measure(
    tmp_path: pathlib.Path,
) -> None:
    """A measure in a grain is the diagnostic form of a real confusion.

    A fact identified by one of its own measurements gains a second row every
    time the same event is measured differently, which is the double counting
    the grain exists to rule out.
    """
    model = build(
        tmp_path,
        {
            "FctX": _fact(
                **{
                    "varda:grain": ["d_key", "amount"],
                    "varda:grain_statement": "one row per thing per amount",
                }
            ),
            "DimThing": dimension(),
        },
    )
    assert "V204" in codes(model)


def test_a_well_formed_grain_is_quiet(tmp_path: pathlib.Path) -> None:
    """The grain rules stay silent on a fact that declares both halves.

    Worth asserting explicitly: five rules now read the grain, and a suite
    that only ever checks that they fire cannot tell an alert validator from
    a noisy one.
    """
    model = build(
        tmp_path,
        {
            "FctX": _fact(
                **{
                    "varda:grain": ["d_key", "ticket"],
                    "varda:grain_statement": "one row per ticket per thing",
                    "varda:fact_type": "TRANSACTION",
                }
            ),
            "DimThing": dimension(),
        },
    )
    fired = codes(model)
    assert not fired & {"V201", "V202", "V203", "V204"}


def test_v105_dimension_without_surrogate_key(
    tmp_path: pathlib.Path,
) -> None:
    table = dimension()
    del table["attributes"]["d_key"]
    model = build(tmp_path, {"DimThing": table})
    assert "V301" in codes(model)


def test_v105_dimension_with_two_surrogate_keys(
    tmp_path: pathlib.Path,
) -> None:
    table = dimension()
    table["attributes"]["other_key"] = {
        "range": "integer",
        "annotations": {"varda:role": "SURROGATE_KEY"},
    }
    model = build(tmp_path, {"DimThing": table})
    found = [f for f in rules.check(model) if f.rule == "V301"]
    assert found
    assert "found 2" in found[0].message


def test_v106_dimension_without_natural_key(tmp_path: pathlib.Path) -> None:
    table = dimension()
    del table["attributes"]["d_id"]
    model = build(tmp_path, {"DimThing": table})
    assert "V302" in codes(model)


def test_v107_fact_without_foreign_key(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per thing that happened",
                },
                "attributes": {
                    "amount": {
                        "range": "decimal",
                        "unit": {"symbol": "EUR"},
                        "annotations": {
                            "varda:role": "MEASURE",
                            "varda:additivity": "ADDITIVE",
                        },
                    }
                },
            }
        },
    )
    assert "V401" in codes(model)


def test_v108_foreign_key_without_target(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per thing that happened",
                },
                "attributes": {
                    "d_key": {
                        "range": "integer",
                        "annotations": {"varda:role": "FOREIGN_KEY"},
                    }
                },
            }
        },
    )
    assert "V402" in codes(model)


def test_v109_foreign_key_target_missing(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per thing that happened",
                },
                "attributes": {
                    "d_key": {
                        "range": "integer",
                        "annotations": {
                            "varda:role": "FOREIGN_KEY",
                            "varda:references": "DimGhost",
                        },
                    }
                },
            }
        },
    )
    assert "V403" in codes(model)


def test_v110_foreign_key_points_at_a_fact(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctA": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per thing that happened",
                    "varda:fact_type": "TRANSACTION",
                },
                "attributes": {
                    "b_key": {
                        "range": "integer",
                        "annotations": {
                            "varda:role": "FOREIGN_KEY",
                            "varda:references": "FctB",
                        },
                    }
                },
            },
            "FctB": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per other thing",
                    "varda:fact_type": "TRANSACTION",
                },
                "attributes": {
                    "d_key": {
                        "range": "integer",
                        "annotations": {
                            "varda:role": "FOREIGN_KEY",
                            "varda:references": "DimThing",
                        },
                    }
                },
            },
            "DimThing": dimension(),
        },
    )
    assert "V404" in codes(model)


def test_v111_fact_without_type(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per thing that happened",
                },
                "attributes": {
                    "d_key": {
                        "range": "integer",
                        "annotations": {
                            "varda:role": "FOREIGN_KEY",
                            "varda:references": "DimThing",
                        },
                    }
                },
            },
            "DimThing": dimension(),
        },
    )
    assert "V501" in codes(model)


def test_v112_surrogate_key_on_a_fact(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per thing that happened",
                    "varda:fact_type": "TRANSACTION",
                },
                "attributes": {
                    "bad_key": {
                        "range": "integer",
                        "annotations": {"varda:role": "SURROGATE_KEY"},
                    },
                    "d_key": {
                        "range": "integer",
                        "annotations": {
                            "varda:role": "FOREIGN_KEY",
                            "varda:references": "DimThing",
                        },
                    },
                },
            },
            "DimThing": dimension(),
        },
    )
    assert "V103" in codes(model)


def test_v113_dimension_without_scd(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {"DimThing": dimension(annotations={"varda:role": "DIMENSION"})},
    )
    assert "V502" in codes(model)


def test_v201_measure_without_additivity(tmp_path: pathlib.Path) -> None:
    table = dimension(annotations={"varda:role": "BRIDGE"})
    table["attributes"]["amount"] = {
        "range": "decimal",
        "annotations": {"varda:role": "MEASURE"},
    }
    model = build(tmp_path, {"BridgeX": table})
    assert "V701" in codes(model)


def test_v202_semi_additive_without_exception(
    tmp_path: pathlib.Path,
) -> None:
    table = dimension(annotations={"varda:role": "BRIDGE"})
    table["attributes"]["amount"] = {
        "range": "decimal",
        "unit": {"symbol": "EUR"},
        "annotations": {
            "varda:role": "MEASURE",
            "varda:additivity": "SEMI_ADDITIVE",
        },
    }
    model = build(tmp_path, {"BridgeX": table})
    assert "V702" in codes(model)


def test_v203_semi_additive_over_is_not_a_key(
    tmp_path: pathlib.Path,
) -> None:
    model = build(
        tmp_path,
        {
            "FctX": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per thing that happened",
                    "varda:fact_type": "PERIODIC_SNAPSHOT",
                },
                "attributes": {
                    "d_key": {
                        "range": "integer",
                        "annotations": {
                            "varda:role": "FOREIGN_KEY",
                            "varda:references": "DimThing",
                        },
                    },
                    "balance": {
                        "range": "decimal",
                        "unit": {"symbol": "EUR"},
                        "annotations": {
                            "varda:role": "MEASURE",
                            "varda:additivity": "SEMI_ADDITIVE",
                            "varda:semi_additive_over": "date_key",
                        },
                    },
                },
            },
            "DimThing": dimension(),
        },
    )
    found = [f for f in rules.check(model) if f.rule == "V703"]
    assert found
    assert "date_key" in found[0].message


def test_v204_measure_on_a_dimension(tmp_path: pathlib.Path) -> None:
    table = dimension()
    table["attributes"]["amount"] = {
        "range": "decimal",
        "unit": {"symbol": "EUR"},
        "annotations": {
            "varda:role": "MEASURE",
            "varda:additivity": "ADDITIVE",
        },
    }
    model = build(tmp_path, {"DimThing": table})
    assert "V704" in codes(model)


def test_v205_measure_without_unit(tmp_path: pathlib.Path) -> None:
    table = dimension(annotations={"varda:role": "BRIDGE"})
    table["attributes"]["amount"] = {
        "range": "decimal",
        "annotations": {
            "varda:role": "MEASURE",
            "varda:additivity": "ADDITIVE",
        },
    }
    model = build(tmp_path, {"BridgeX": table})
    assert "V705" in codes(model)


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ({"symbol": "EUR"}, "EUR"),
        ({"ucum_code": "kg"}, "kg"),
        ({"abbreviation": "hr"}, "hr"),
        ({"descriptive_name": "kilogram"}, "kilogram"),
        # The symbol is what a reader recognizes, so it wins over the code.
        ({"symbol": "kg", "ucum_code": "kg.m2"}, "kg"),
    ],
)
def test_unit_reads_every_way_linkml_names_one(
    tmp_path: pathlib.Path,
    unit: dict[str, str],
    expected: str,
) -> None:
    """A unit is LinkML's own, and it may be written several ways."""
    table = dimension(annotations={"varda:role": "BRIDGE"})
    table["attributes"]["amount"] = {
        "range": "decimal",
        "unit": unit,
        "annotations": {
            "varda:role": "MEASURE",
            "varda:additivity": "ADDITIVE",
        },
    }
    model = build(tmp_path, {"BridgeX": table})
    bridge = model.table("BridgeX")
    assert bridge is not None
    column = bridge.column("amount")
    assert column is not None
    assert column.unit == expected
    assert "V705" not in codes(model)


def test_a_unit_naming_nothing_is_undeclared(tmp_path: pathlib.Path) -> None:
    """A unit carrying only a derivation names no unit a reader could use."""
    table = dimension(annotations={"varda:role": "BRIDGE"})
    table["attributes"]["amount"] = {
        "range": "decimal",
        "unit": {"derivation": "price times quantity"},
        "annotations": {
            "varda:role": "MEASURE",
            "varda:additivity": "ADDITIVE",
        },
    }
    model = build(tmp_path, {"BridgeX": table})
    assert "V705" in codes(model)


def test_v206_fact_without_measures(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per thing that happened",
                    "varda:fact_type": "TRANSACTION",
                },
                "attributes": {
                    "d_key": {
                        "range": "integer",
                        "annotations": {
                            "varda:role": "FOREIGN_KEY",
                            "varda:references": "DimThing",
                        },
                    }
                },
            },
            "DimThing": dimension(),
        },
    )
    assert "V706" in codes(model)


def test_v206_silent_when_declared_factless(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": {
                "annotations": {
                    "varda:role": "FACT",
                    "varda:grain_statement": "one row per thing that happened",
                    "varda:fact_type": "FACTLESS",
                },
                "attributes": {
                    "d_key": {
                        "range": "integer",
                        "annotations": {
                            "varda:role": "FOREIGN_KEY",
                            "varda:references": "DimThing",
                        },
                    }
                },
            },
            "DimThing": dimension(),
        },
    )
    assert "V706" not in codes(model)


# ---------------------------------------------------------------------------
# The rule set itself
# ---------------------------------------------------------------------------


def test_rule_codes_are_unique() -> None:
    all_codes = [c for c, *_ in rules.all_rules()]
    assert len(all_codes) == len(set(all_codes))


def test_rule_set_rejects_a_foreign_code() -> None:
    rs = RuleSet(tag="V")
    with pytest.raises(ValueError, match="does not belong"):
        rs.rule("X001", "error", "nope")


def test_rule_set_rejects_a_duplicate_code() -> None:
    rs = RuleSet(tag="V")
    rs.rule("V900", "error", "first")(lambda _m: iter(()))
    with pytest.raises(ValueError, match="already registered"):
        rs.rule("V900", "error", "second")


def test_rule_set_rejects_an_unknown_severity() -> None:
    rs = RuleSet(tag="V")
    with pytest.raises(ValueError, match="not one of"):
        rs.rule("V901", "critical", "nope")


def test_an_info_rule_never_stops_a_run(tmp_path: pathlib.Path) -> None:
    """The third severity, which the core ships and does not use.

    All 48 Varda rules are `error` or `warning`. `info` is there for
    extensions, which have reason to report a house convention worth naming
    in a build log without failing it — so `extending.md` says it never
    stops a run, and this is what makes that true.
    """
    rs = RuleSet(tag="LL")
    rs.rule("LL001", "info", "Noted")(
        lambda m: iter(
            [rules.Finding("LL001", "info", str(t), "noted") for t in m.tables]
        )
    )
    model = build(tmp_path, {"DimThing": dimension()})
    with registry.using(_bare("l", "lll", rule_tag="LL", rules=rs)):
        found = rules.check(model)
        assert "LL001" in {f.rule for f in found}
        path = tmp_path / "t.yaml"
        assert cli.main(["check", str(path), "--strict"]) == 0


def test_unknown_codes_finds_stale_exemptions() -> None:
    assert rules.unknown_codes(["V001", "V999"]) == ["V999"]


def test_exemptions_skip_a_rule(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {"DimThing": dimension(annotations={"varda:role": "DIMENSION"})},
    )
    assert "V502" in codes(model)
    fired = {f.rule for f in rules.check(model, exemptions=["V502"])}
    assert "V502" not in fired


# ---------------------------------------------------------------------------
# The registry and the extension mechanism
# ---------------------------------------------------------------------------


def test_varda_is_itself_an_extension() -> None:
    """The core goes through the public interface.

    If this stops being true, the interface has become a facade and a third
    party will be the one to discover it.
    """
    core = registry.varda_extension()
    assert isinstance(core, Extension)
    assert core.prefix == "varda"
    assert core.rules is not None


def test_model_objects_can_go_in_a_set() -> None:
    """A rule asking about columns should be able to use columns.

    The generated `__hash__` on these dataclasses reached into a
    `SlotDefinition` and raised `TypeError: unhashable type`, so every rule
    that needed a set of columns built a set of names instead — answering an
    identity question through strings, which is the indirection this layer
    exists to remove.
    """
    model = DimensionalModel.load(SNOWFLAKE, importmap=registry.importmap())
    table = model.dimensions[0]
    assert len({*table.columns}) == len(table.columns)
    assert len({*model.tables}) == len(model.tables)
    hierarchy = next(h for t in model.tables for h in t.hierarchies)
    assert len({*hierarchy.resolved}) == len(hierarchy.resolved)


def test_model_objects_are_the_same_object_each_time() -> None:
    """Identity is only worth having if instances are stable.

    `hierarchies` and `resolved` were plain properties and rebuilt on every
    access — 51 rebuilds across 8 tables in one `check()` — so two `Level`
    objects for one level were never the same object and a set of them
    would have held duplicates.
    """
    model = DimensionalModel.load(SNOWFLAKE, importmap=registry.importmap())
    table = model.dimensions[0]
    assert table.columns[0] is table.columns[0]
    assert model.tables[0] is model.tables[0]
    owner = next(t for t in model.tables if t.hierarchies)
    assert owner.hierarchies[0] is owner.hierarchies[0]
    hierarchy = owner.hierarchies[0]
    assert hierarchy.resolved is hierarchy.resolved


def test_declared_annotations_are_namespaced() -> None:
    assert "varda:role" in registry.declared_annotations("table")
    assert "varda:grain" in registry.declared_annotations("table")
    assert "varda:grain" not in registry.declared_annotations("column")


def test_permitted_values() -> None:
    assert registry.permitted("TableRole") == (
        "FACT",
        "DIMENSION",
        "BRIDGE",
    )


def test_annotation_enum_resolves() -> None:
    assert registry.annotation_enum("table", "varda:role") == "TableRole"
    assert registry.annotation_enum("table", "varda:grain") is None


def test_importmap_points_at_the_profile() -> None:
    assert "varda" in registry.importmap()


def test_extension_activates(tmp_path: pathlib.Path) -> None:
    from acme_ext import EXTENSION  # noqa: PLC0415

    table = dimension()
    table["annotations"]["acme:cost_center"] = "CC-4471"
    with registry.using(EXTENSION):
        model = build(tmp_path, {"DimThing": table})
        fired = codes(model)
        assert "V001" not in fired  # acme:cost_center is now declared
        assert "ACME101" not in fired
        assert "acme" in registry.prefixes()
        assert "acme:cost_center" in registry.declared_annotations("table")


def test_extension_rule_fires(tmp_path: pathlib.Path) -> None:
    from acme_ext import EXTENSION  # noqa: PLC0415

    with registry.using(EXTENSION):
        model = build(tmp_path, {"DimThing": dimension()})
        assert "ACME101" in codes(model)


def test_extension_enum_is_checked(tmp_path: pathlib.Path) -> None:
    """An extension's own enums are enforced by V002.

    The core's validator polices the extension's vocabulary, which is what
    makes writing an extension worth doing rather than just agreeing on a
    convention.
    """
    from acme_ext import EXTENSION  # noqa: PLC0415

    table = dimension()
    table["annotations"]["acme:cost_center"] = "CC-1"
    table["attributes"]["d_id"]["annotations"]["acme:sensitivity"] = "secret"
    with registry.using(EXTENSION):
        model = build(tmp_path, {"DimThing": table})
        found = [f for f in rules.check(model) if f.rule == "V002"]
        assert found
        assert "Sensitivity" in found[0].message


def test_extension_severity_default_applies(tmp_path: pathlib.Path) -> None:
    """Acme raises V705 from warning to error."""
    from acme_ext import EXTENSION  # noqa: PLC0415

    table = dimension(annotations={"varda:role": "BRIDGE"})
    table["annotations"]["acme:cost_center"] = "CC-1"
    table["attributes"]["amount"] = {
        "range": "decimal",
        "annotations": {
            "varda:role": "MEASURE",
            "varda:additivity": "ADDITIVE",
        },
    }
    with registry.using(EXTENSION):
        model = build(tmp_path, {"BridgeX": table})
        found = [f for f in rules.check(model) if f.rule == "V705"]
        assert found
        assert found[0].severity == "error"


# ---------------------------------------------------------------------------
# Extension validation — the failures that cannot be undone once shipped
# ---------------------------------------------------------------------------


def _bare(name: str, prefix: str, **kw: Any) -> Extension:
    return Extension(name=name, prefix=prefix, origin="test", **kw)


def activate(*exts: Extension) -> None:
    """Activate extensions and immediately deactivate them.

    Every validation test below asserts on what happens when the registry is
    asked to accept a set of extensions, and the answer arrives on the way
    in. Naming that here keeps each test to a single `with`.
    """
    with registry.using(*exts):
        pass


def test_reserved_prefix_is_refused() -> None:
    with pytest.raises(ExtensionError, match="reserved"):
        activate(_bare("evil", "skos"))


def test_duplicate_prefix_is_refused() -> None:
    with pytest.raises(ExtensionError, match="claimed by both"):
        activate(_bare("a", "dup"), _bare("b", "dup"))


def test_malformed_prefix_is_refused() -> None:
    with pytest.raises(ExtensionError, match="must be lowercase"):
        activate(_bare("a", "Bad-Prefix"))


def test_duplicate_rule_tag_is_refused() -> None:
    with pytest.raises(ExtensionError, match="rule tag"):
        activate(
            _bare("a", "one", rule_tag="X"), _bare("b", "two", rule_tag="X")
        )


def test_rule_tag_must_match_its_rule_set() -> None:
    with pytest.raises(ExtensionError, match="but its RuleSet says"):
        activate(_bare("a", "one", rule_tag="AAA", rules=RuleSet(tag="BBB")))


def test_duplicate_rule_code_is_refused() -> None:
    left = RuleSet(tag="LL")
    right = RuleSet(tag="RR")
    left.rule("LL001", "error", "left")(lambda _m: iter(()))
    # Force a collision the tag check cannot catch, by appending directly.
    right.rules.append(("LL001", "error", "right", lambda _m: iter(())))
    with pytest.raises(ExtensionError, match="registered by both"):
        activate(
            _bare("l", "lft", rule_tag="LL", rules=left),
            _bare("r", "rgt", rule_tag="RR", rules=right),
        )


def test_colliding_artifact_path_is_refused() -> None:
    def run(_ctx: Any) -> dict[str, str]:
        return {"sql/mart.sql": ""}

    gen = Generator(name="mine", artifacts=("sql/mart.sql",), run=run)
    with pytest.raises(ExtensionError, match="silently overwrite"):
        activate(_bare("a", "one", generators=(gen,)))


def test_colliding_generator_name_is_refused() -> None:
    def run(_ctx: Any) -> dict[str, str]:
        return {"other/x.txt": ""}

    gen = Generator(name="sql", artifacts=("other/x.txt",), run=run)
    with pytest.raises(ExtensionError, match="generator 'sql'"):
        activate(_bare("a", "one", generators=(gen,)))


@pytest.mark.parametrize(
    "path",
    [
        "../escaped.sql",
        "sql/../../escaped.sql",
        "/tmp/escaped.sql",  # noqa: S108 — the point is that it is refused
        "C:\\out\\escaped.sql",
        "",
    ],
)
def test_an_artifact_path_that_leaves_the_output_tree_is_refused(
    path: str,
) -> None:
    """Declaring a path is what makes it checkable, so check it.

    `cli.generate` joins a declared path straight onto `--out`, and until
    this was refused the join was the only thing constraining it: a
    generator declaring `../escaped.sql` wrote a file outside the tree and
    the run reported success. The Windows form is refused on POSIX too,
    because the same extension is installed on both and the answer should
    not depend on which machine loaded it.
    """

    def run(_ctx: Any) -> dict[str, str]:
        return {path: ""}

    gen = Generator(name="esc", artifacts=(path,), run=run)
    with pytest.raises(ExtensionError, match="stay inside"):
        activate(_bare("a", "one", generators=(gen,)))


def test_nothing_is_written_outside_the_output_directory(
    tmp_path: pathlib.Path,
) -> None:
    """The end-to-end form of the rule above, through the CLI."""

    def run(_ctx: Any) -> dict[str, str]:
        return {"../escaped.sql": "-- no\n"}

    gen = Generator(name="esc", artifacts=("../escaped.sql",), run=run)
    out = tmp_path / "out"
    with pytest.raises(ExtensionError):
        activate(_bare("a", "one", generators=(gen,)))
    assert not (tmp_path / "escaped.sql").exists()
    assert not out.exists()


def test_every_cached_lookup_is_reset() -> None:
    """A reset that stops short is a cache that silently goes stale.

    `annotation_shape` and `structured_shape` were both missing from the
    hand-written list this replaces, so a shape looked up before an
    extension was active stayed `None` afterwards and V004 quietly stopped
    checking that extension's structured annotations. The list is now built
    rather than listed; this asserts the build finds everything.
    """
    owned = {
        name
        for name, obj in vars(registry).items()
        if hasattr(obj, "cache_clear")
        and getattr(obj, "__module__", None) == registry.__name__
    }
    reset = {fn.__name__ for fn in registry._cached()}
    assert owned - reset == registry._KEEP_CACHED


def test_a_structured_shape_is_seen_after_an_extension_is_activated() -> None:
    """The failure the reset above exists to prevent, from the outside."""
    from acme_ext import EXTENSION  # noqa: PLC0415

    assert registry.structured_shape("acme", "AcmeTableAnnotations") is None
    with registry.using(EXTENSION):
        shape = registry.structured_shape("acme", "AcmeTableAnnotations")
    assert shape == {"cost_center": "string", "retention_days": "integer"}


def test_profile_prefix_must_match(tmp_path: pathlib.Path) -> None:
    profile = tmp_path / "p.yaml"
    profile.write_text(
        textwrap.dedent("""
            id: https://example.org/p
            name: p
            prefixes:
              linkml: https://w3id.org/linkml/
              other: https://example.org/other/
            default_prefix: other
            imports:
              - linkml:types
        """).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ExtensionError, match="default_prefix"):
        activate(_bare("a", "mine", profile=profile))


def test_extension_may_not_redeclare_a_varda_enum(
    tmp_path: pathlib.Path,
) -> None:
    """The design's central prohibition, enforced mechanically.

    An extension redeclaring `Additivity` is not extending the vocabulary, it
    is forking it — and the fork is invisible, because both schemas parse.
    """
    profile = tmp_path / "p.yaml"
    profile.write_text(
        textwrap.dedent("""
            id: https://example.org/p
            name: p
            prefixes:
              linkml: https://w3id.org/linkml/
              mine: https://example.org/mine/
            default_prefix: mine
            imports:
              - linkml:types
            enums:
              Additivity:
                permissible_values:
                  whenever_you_like:
                    description: no
        """).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ExtensionError, match="never redefine"):
        activate(_bare("a", "mine", profile=profile))


def test_severity_disagreement_is_refused() -> None:
    """Two extensions cannot both decide one rule's severity.

    Resolving by load order would make the answer depend on discovery order,
    which is a difference between two machines that nothing in either
    repository explains.
    """
    with pytest.raises(ExtensionError, match="Nothing here can adjudicate"):
        activate(
            _bare("group", "grp", severity_defaults={"V705": "error"}),
            _bare("team", "tm", severity_defaults={"V705": "info"}),
        )


def test_agreeing_severity_defaults_are_fine() -> None:
    with registry.using(
        _bare("group", "grp", severity_defaults={"V705": "error"}),
        _bare("team", "tm", severity_defaults={"V705": "error"}),
    ):
        assert registry.severities()["V705"] == "error"


def test_unknown_severity_is_refused() -> None:
    with pytest.raises(ExtensionError, match="not one of"):
        activate(_bare("a", "one", severity_defaults={"V705": "loud"}))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def executes(sql: str) -> None:
    """Run the DDL against a real database, and fail with its own message.

    The SQL generator's whole contract is that its output runs, and reading
    the output as text cannot check it. A CREATE TABLE emitted before the
    table it references, a column named `select`, and two columns sharing a
    physical name all read as reasonable SQL and all fail here — none of
    them is a case anyone thinks to write a string assertion for.

    DuckDB rather than a server: one wheel, in-memory, and strict about the
    parts that matter. It is not the warehouse anyone ships to, so this
    checks that the DDL is legal, never that it is optimal.
    """
    try:
        duckdb.connect().execute(sql)
    except duckdb.Error as exc:
        pytest.fail(f"generated DDL did not execute: {exc}")


def test_sql_is_deterministic() -> None:
    model = DimensionalModel.load(RETAIL)
    assert generate_sql(model) == generate_sql(model)


def test_the_example_ddl_executes() -> None:
    """The shipped example runs against an empty database."""
    executes(generate_sql(DimensionalModel.load(RETAIL)))


def test_a_snowflake_ddl_executes(tmp_path: pathlib.Path) -> None:
    """A dimension referencing another runs, not only sorts correctly.

    The end-to-end half of `test_sql_creates_a_snowflake_in_dependency_order`:
    that one says the order is right, this one says the file works.
    """
    model = build(
        tmp_path,
        _snowflake(["country_key.country_name", "state_key.state_name"]),
    )
    executes(generate_sql(model))


def test_reserved_words_survive_as_identifiers(
    tmp_path: pathlib.Path,
) -> None:
    """A column named after a SQL keyword still produces a legal table.

    `select` passes every rule — it is a perfectly good slot name — and
    unquoted it produced a file no parser accepts. Identifiers are quoted,
    so the only thing that decides a legal name is the database.
    """
    table = dimension()
    for name in ("select", "from", "order", "group", "table"):
        table["attributes"][name] = {"annotations": {"varda:role": "ATTRIBUTE"}}
    executes(generate_sql(build(tmp_path, {"DimKeyword": table})))


def test_sql_orders_dimensions_before_facts() -> None:
    sql = generate_sql(DimensionalModel.load(RETAIL))
    assert sql.index('"dim_customer" (') < sql.index('"fct_sale" (')


def _creates_before_references(sql: str, model: DimensionalModel) -> None:
    """Assert every table is created after everything it references.

    Written over the model rather than against named pairs, because the
    ordering bug this guards was invisible to a test that asserted one pair:
    dimensions did precede facts while a dimension preceded the dimension it
    referenced.
    """
    where = {t.name: sql.index(f'"{t.physical}" (') for t in model.tables}
    for table in model.tables:
        for column in table.foreign_keys:
            target = model.table(column.references or "")
            if target is None or target.name == table.name:
                continue
            assert where[target.name] < where[table.name], (
                f"{table.physical} references {target.physical}, "
                f"which is created after it"
            )


def test_sql_creates_a_snowflake_in_dependency_order(
    tmp_path: pathlib.Path,
) -> None:
    """A dimension referencing another is created after it.

    Sorting dimensions by name puts DimCity first and DimState third, so the
    file created a table whose target did not exist and stopped there. The
    reference form of a hierarchy level exists for exactly this arrangement,
    so it is not a corner the generator can decline to handle.
    """
    model = build(
        tmp_path,
        _snowflake(["country_key.country_name", "state_key.state_name"]),
    )
    _creates_before_references(generate_sql(model), model)


def test_sql_orders_a_flat_star_as_before() -> None:
    """Dependency ordering breaks ties by name, so a star does not move."""
    sql = generate_sql(DimensionalModel.load(RETAIL))
    tables = ["dim_customer", "dim_date", "dim_product", "dim_segment"]
    positions = [sql.index(f'"{name}" (') for name in tables]
    assert positions == sorted(positions)


def test_sql_allows_a_dimension_to_reference_itself(
    tmp_path: pathlib.Path,
) -> None:
    """A recursive dimension is not a cycle.

    `FOREIGN KEY (manager_key) REFERENCES dim_employee` inside its own
    CREATE TABLE is legal SQL, and a reporting line is a normal thing to
    model, so refusing it as unorderable would reject a working design.
    """
    employee = dimension()
    employee["attributes"]["manager_key"] = {
        "range": "integer",
        "annotations": {
            "varda:role": "FOREIGN_KEY",
            "varda:references": "DimEmployee",
        },
    }
    sql = generate_sql(build(tmp_path, {"DimEmployee": employee}))
    assert 'FOREIGN KEY ("manager_key")' in sql
    assert 'REFERENCES "mart"."dim_employee" ("d_key")' in sql


def test_sql_refuses_a_cycle_between_dimensions(
    tmp_path: pathlib.Path,
) -> None:
    """Two dimensions referencing each other have no legal emission order.

    Raising names both tables. Emitting them in name order would produce a
    file that fails in the database instead, which is the failure this whole
    ordering exists to prevent.
    """
    left, right = dimension(), dimension()
    left["attributes"]["r_key"] = {
        "range": "integer",
        "annotations": {
            "varda:role": "FOREIGN_KEY",
            "varda:references": "DimRight",
        },
    }
    right["attributes"]["l_key"] = {
        "range": "integer",
        "annotations": {
            "varda:role": "FOREIGN_KEY",
            "varda:references": "DimLeft",
        },
    }
    model = build(tmp_path, {"DimLeft": left, "DimRight": right})
    with pytest.raises(GenerationError, match="DimLeft, DimRight"):
        generate_sql(model)


def test_sql_emits_foreign_keys() -> None:
    sql = generate_sql(DimensionalModel.load(RETAIL))
    assert (
        'FOREIGN KEY ("customer_key")\n'
        '        REFERENCES "mart"."dim_customer" ("customer_key")' in sql
    )


def test_sql_lines_fit_the_limit() -> None:
    model = DimensionalModel.load(RETAIL)
    written = [
        generate_sql(model, "mart", dialect, level)
        for dialect in gen_sql.DIALECTS
        for level in gen_sql.LEVELS
    ] + [generate_assertions(model)]
    # Every level and every dialect, because the comment block a weaker
    # level writes and the predicate a compound grain produces are both
    # longer than the constraint they stand in for.
    assert max(len(line) for out in written for line in out.splitlines()) <= 80


def test_unmapped_range_raises(tmp_path: pathlib.Path) -> None:
    """An unknown range must stop generation, not default to text.

    A column silently typed as text is a bug that surfaces years later, as a
    comparison that does not do what it looks like.
    """
    table = dimension()
    table["attributes"]["odd"] = {
        "range": "curie",
        "annotations": {"varda:role": "ATTRIBUTE"},
    }
    model = build(tmp_path, {"DimThing": table})
    with pytest.raises(GenerationError, match="no SQL mapping"):
        generate_sql(model)


def test_generate_writes_declared_artifacts(tmp_path: pathlib.Path) -> None:
    code = cli.main(["generate", str(RETAIL), "--out", str(tmp_path / "out")])
    assert code == 0
    assert (tmp_path / "out" / "sql" / "mart.sql").is_file()
    assert (tmp_path / "out" / "docs" / "model.md").is_file()


def _nonconforming(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write a model with one error and one warning, and return its path."""
    table = dimension()
    table["attributes"]["extra_key"] = {
        "range": "integer",
        "annotations": {"varda:role": "SURROGATE_KEY"},  # V301: two of them
    }
    del table["annotations"]["varda:scd"]  # V502: a warning
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "https://example.org/t",
                "name": "t",
                "default_prefix": "t",
                "default_range": "string",
                "imports": ["linkml:types"],
                "prefixes": {
                    "linkml": "https://w3id.org/linkml/",
                    "varda": "https://w3id.org/varda/",
                },
                "classes": {"DimThing": table},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_generate_refuses_a_nonconforming_model(
    tmp_path: pathlib.Path,
) -> None:
    """Errors stop the run, and nothing reaches the disk.

    Artifacts from a model that does not conform look finished and are not.
    A grain naming a column that does not exist emits a table with no
    uniqueness at all, and two surrogate keys emit two PRIMARY KEY clauses
    that no database accepts.
    """
    out = tmp_path / "out"
    assert (
        cli.main(["generate", str(_nonconforming(tmp_path)), "--out", str(out)])
        == 1
    )
    assert not out.exists()


def test_generate_force_writes_anyway(tmp_path: pathlib.Path) -> None:
    """`--force` is for somebody mid-refactor who wants to see the output."""
    out = tmp_path / "out"
    code = cli.main(
        [
            "generate",
            str(_nonconforming(tmp_path)),
            "--out",
            str(out),
            "--force",
        ]
    )
    assert code == 0
    assert (out / "sql" / "mart.sql").is_file()


def test_generate_exempt_unblocks_one_rule(tmp_path: pathlib.Path) -> None:
    """Exempting the rule that blocks is not the same as forcing past it.

    The exemptions `check` honors are the exemptions `generate` honors, or
    the two commands disagree about whether a model is fit to build from.
    """
    out = tmp_path / "out"
    args = ["generate", str(_nonconforming(tmp_path)), "--out", str(out)]
    assert cli.main(args) == 1
    assert cli.main([*args, "--exempt", "V301"]) == 0


def test_generate_strict_stops_on_warnings(tmp_path: pathlib.Path) -> None:
    """`--strict` means the same thing here as it does in `check`."""
    out = tmp_path / "out"
    args = ["generate", str(_nonconforming(tmp_path)), "--out", str(out)]
    assert cli.main([*args, "--exempt", "V301"]) == 0
    assert cli.main([*args, "--exempt", "V301", "--strict"]) == 1


def test_generate_fails_closed(tmp_path: pathlib.Path) -> None:
    """A failing generator leaves nothing behind.

    A half-generated estate is worse than none: it looks complete, and the
    stale parts are the ones nobody thinks to check. The failing generator is
    registered *after* the two that succeed, so the test proves collection
    happens before any write rather than that ordering saved us.
    """

    def boom(_ctx: Any) -> dict[str, str]:
        msg = "deliberate"
        raise RuntimeError(msg)

    gen = Generator(name="zzz", artifacts=("zzz/out.txt",), run=boom)
    out = tmp_path / "out"
    with registry.using(_bare("a", "one", generators=(gen,))):
        code = cli.main(["generate", str(RETAIL), "--out", str(out)])
    assert code == 1
    assert not out.exists()


def test_generate_rejects_an_undeclared_path(tmp_path: pathlib.Path) -> None:
    def sneaky(_ctx: Any) -> dict[str, str]:
        return {"declared.txt": "a", "undeclared.txt": "b"}

    gen = Generator(name="zzz", artifacts=("declared.txt",), run=sneaky)
    out = tmp_path / "out"
    with registry.using(_bare("a", "one", generators=(gen,))):
        code = cli.main(["generate", str(RETAIL), "--out", str(out)])
    assert code == 1
    assert not out.exists()


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_check_passes_on_the_example() -> None:
    assert cli.main(["check", str(RETAIL)]) == 0


def test_check_fails_on_errors(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "https://example.org/t",
                "name": "t",
                "default_prefix": "t",
                "default_range": "string",
                "imports": ["linkml:types"],
                "prefixes": {"linkml": "https://w3id.org/linkml/"},
                "classes": {"DimThing": {"annotations": {"varda:role": "x"}}},
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["check", str(path)]) == 1


def test_check_strict_fails_on_warnings(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "warn.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "https://example.org/t",
                "name": "t",
                "default_prefix": "t",
                "default_range": "string",
                "imports": ["linkml:types"],
                "prefixes": {"linkml": "https://w3id.org/linkml/"},
                "classes": {
                    "DimThing": dimension(
                        annotations={"varda:role": "DIMENSION"}
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["check", str(path)]) == 0
    assert cli.main(["check", str(path), "--strict"]) == 1


def test_check_strict_fails_on_a_stale_exemption() -> None:
    assert cli.main(["check", str(RETAIL), "--exempt", "V999"]) == 0
    assert cli.main(["check", str(RETAIL), "--exempt", "V999", "--strict"]) == 1


def test_generate_reports_a_stale_exemption_too(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command that writes files was the one saying nothing.

    A suppression that has outlived its rule is a maintenance finding
    wherever it is read, and `_blocking` promises the two commands cannot
    disagree about whether a model is fit to generate from.
    """
    args = ["generate", str(RETAIL), "--out", str(tmp_path), "--exempt", "V999"]
    assert cli.main(args) == 0
    assert "names no registered rule: V999" in capsys.readouterr().err
    assert cli.main([*args, "--strict"]) == 1


def test_unknown_generator_is_a_usage_error() -> None:
    assert cli.main(["generate", str(RETAIL), "--only", "nope"]) == 2


def test_missing_file_is_a_usage_error() -> None:
    assert cli.main(["check", "no/such/model.yaml"]) == 2


def test_rules_command_lists_every_rule(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["rules"]) == 0
    out = capsys.readouterr().out
    assert "V001" in out
    assert f"{len(rules.all_rules())} rules" in out


def test_ext_command_describes_varda(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["ext"]) == 0
    out = capsys.readouterr().out
    assert "varda" in out
    assert "varda:grain" in out


def test_importmap_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["importmap"]) == 0
    assert "varda=" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# varda.toml — the extension route that needs no Python at all
# ---------------------------------------------------------------------------


def write_config(
    tmp_path: pathlib.Path, body: str, profile: str | None = None
) -> pathlib.Path:
    """Write a varda.toml, and optionally a profile beside it."""
    if profile is not None:
        (tmp_path / "p.yaml").write_text(
            textwrap.dedent(profile).strip(), encoding="utf-8"
        )
    path = tmp_path / "varda.toml"
    path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return path


PROFILE_YAML = """
    id: https://example.org/fin
    name: fin
    version: 2.0.0
    prefixes:
      linkml: https://w3id.org/linkml/
      fin: https://example.org/fin/
    default_prefix: fin
    default_range: string
    imports:
      - linkml:types
    enums:
      Ledger:
        permissible_values:
          statutory: {}
          management: {}
    classes:
      FinTableAnnotations:
        annotations:
          fin:applies_to: table
        attributes:
          ledger:
            range: Ledger
"""


def test_toml_declares_an_extension_without_python(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The smallest useful extension is a YAML file and three lines of TOML.

    An organization that only wants extra vocabulary should not have to
    publish a package to get it, because requiring that is what pushes people
    into using undeclared annotations instead.
    """
    path = write_config(
        tmp_path,
        """
        [[extension]]
        name = "fin"
        prefix = "fin"
        profile = "p.yaml"
        """,
        profile=PROFILE_YAML,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    assert "fin" in registry.prefixes()
    assert "fin:ledger" in registry.declared_annotations("table")


def test_toml_extension_vocabulary_is_enforced(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        """
        [[extension]]
        name = "fin"
        prefix = "fin"
        profile = "p.yaml"
        """,
        profile=PROFILE_YAML,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    model = build(
        tmp_path,
        {
            "DimThing": dimension(
                annotations={
                    "varda:role": "DIMENSION",
                    "varda:scd": "TYPE_1",
                    "fin:ledger": "invented",
                }
            )
        },
    )
    found = [f for f in rules.check(model) if f.rule == "V002"]
    assert found
    assert "Ledger" in found[0].message


def test_toml_severity_beats_an_extension(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repository's word is final.

    An extension's `severity_defaults` is an opinion shipped by a party who
    cannot see this codebase; `varda.toml` is written by people who can.
    """
    from acme_ext import EXTENSION  # noqa: PLC0415

    path = write_config(
        tmp_path,
        """
        [severity]
        V705 = "info"
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    with registry.using(EXTENSION):
        assert EXTENSION.severity_defaults["V705"] == "error"
        assert registry.severities()["V705"] == "info"


def test_toml_exemptions_are_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        """
        exempt = ["V502", "V705"]
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    assert registry.exemptions() == ["V502", "V705"]


def test_toml_unknown_key_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`exempts` for `exempt` suppresses nothing and reads like it does.

    V001's argument one layer up. A misspelled annotation is a constraint
    that silently never applies; a misspelled config key is a rule
    suppressed nowhere, and the file is small enough that saying so costs
    nothing.
    """
    path = write_config(
        tmp_path,
        """
        exempts = ["V502"]
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    with pytest.raises(ExtensionError, match="unknown key"):
        registry.exemptions()


def test_toml_unknown_severity_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo here used to end the run in a KeyError traceback.

    The config file has the final word over an extension's severity
    defaults, and having the final word is not a reason to go unchecked:
    the value reached `Finding.__str__`, which indexes a closed three-key
    table.
    """
    path = write_config(
        tmp_path,
        """
        [severity]
        V101 = "eror"
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    with pytest.raises(ExtensionError, match="not one of"):
        registry.severities()


def test_toml_severity_for_a_retired_rule_is_reported(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An override nobody applies is the same silence as a stale exemption."""
    path = write_config(
        tmp_path,
        """
        [severity]
        V999 = "error"
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    assert cli.main(["check", str(RETAIL)]) == 0
    assert "severity override names no registered rule" in (
        capsys.readouterr().err
    )


# ---------------------------------------------------------------------------
# Distribution — the routes a shipped extension takes
# ---------------------------------------------------------------------------
#
# `registry` names three ways an extension becomes active and SPEC lists
# distribution as the fourth seam, stating each is covered by a test. Two of
# the three had never run: the entry point, which is what a *published*
# extension uses and the only route whose failure cannot be found locally,
# and `extensions = ["module"]`, which is the same import by another door.
# Everything below reaches them the way a real installation would.


@dataclasses.dataclass(frozen=True)
class _FakeEntryPoint:
    """One `varda.extensions` entry point, as `entry_points` yields them."""

    name: str
    value: Any
    fails: BaseException | None = None

    def load(self) -> Any:
        if self.fails is not None:
            raise self.fails
        return self.value


def _advertise(
    monkeypatch: pytest.MonkeyPatch, *points: _FakeEntryPoint
) -> None:
    """Make the registry see these entry points and nothing else."""
    monkeypatch.setattr(
        registry, "entry_points", lambda group: points if group else ()
    )
    registry.reset_caches()


def test_an_entry_point_extension_becomes_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route a published extension takes, exercised end to end."""
    from acme_ext import EXTENSION  # noqa: PLC0415

    _advertise(monkeypatch, _FakeEntryPoint("acme", EXTENSION))
    active = {e.prefix: e for e in registry.extensions()}
    assert "acme" in active
    assert "ACME101" in {code for code, *_ in rules.all_rules()}
    assert "acme:cost_center" in registry.declared_annotations("table")


def test_an_entry_point_records_where_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`varda ext` has to be able to say why something is loaded.

    An extension nobody can account for is one nobody can remove.
    """
    # Not `_bare`, which stamps an origin of its own — and an extension that
    # already names where it came from keeps it, which is the other half of
    # the behavior being pinned here.
    plain = Extension(name="fin", prefix="fin")
    _advertise(monkeypatch, _FakeEntryPoint("finance", plain))
    found = next(e for e in registry.extensions() if e.prefix == "fin")
    assert found.origin == "entry point finance"


def test_an_extension_loads_after_its_profile_has_been_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A singleton whose cached profile has been touched still loads.

    An extension is a module-level singleton, and `profile_view` and
    `profile_version` are `cached_property` — they write their results into
    the instance's `__dict__`. Re-creating the extension from that dict to
    stamp its origin therefore raised `TypeError: got an unexpected keyword
    argument 'profile_view'` the moment anything had read the parsed
    profile, and which invocation hit it depended on what had run first in
    the process.
    """
    from acme_ext import EXTENSION  # noqa: PLC0415

    assert EXTENSION.profile_version == "1.2.0"  # populates the cache
    _advertise(monkeypatch, _FakeEntryPoint("acme", EXTENSION))
    assert "acme" in {e.prefix for e in registry.extensions()}


def test_an_entry_point_that_is_not_an_extension_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advertise(monkeypatch, _FakeEntryPoint("bogus", object()))
    with pytest.raises(ExtensionError, match="not an Extension"):
        registry.extensions()


def test_an_entry_point_that_will_not_load_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An extension that silently fails to load looks like one that passes.

    Every rule it would have run is a rule nobody notices did not run, which
    is why this raises rather than skipping the entry point.
    """
    boom = _FakeEntryPoint("broken", None, fails=ImportError("no module"))
    _advertise(monkeypatch, boom)
    with pytest.raises(ExtensionError, match="could not be loaded"):
        registry.extensions()


def test_toml_extensions_import_a_module(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same import, reached through the config file instead."""
    path = write_config(
        tmp_path,
        """
        extensions = ["acme_ext"]
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    active = {e.prefix for e in registry.extensions()}
    assert "acme" in active


def test_toml_extensions_take_a_named_attribute(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`module:NAME` for a package exporting more than one."""
    path = write_config(
        tmp_path,
        """
        extensions = ["acme_ext:EXTENSION"]
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    assert "acme" in {e.prefix for e in registry.extensions()}


def test_toml_extensions_reject_a_missing_module(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        """
        extensions = ["no_such_extension_module"]
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    with pytest.raises(ExtensionError, match="cannot import"):
        registry.extensions()


def test_toml_extensions_reject_a_name_that_is_not_an_extension(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        """
        extensions = ["acme_ext:RULES"]
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    with pytest.raises(ExtensionError, match="does not name an Extension"):
        registry.extensions()


def test_toml_missing_profile_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        """
        [[extension]]
        name = "fin"
        prefix = "fin"
        profile = "nowhere.yaml"
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    with pytest.raises(ExtensionError, match="does not exist"):
        registry.extensions()


def test_toml_incomplete_extension_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        """
        [[extension]]
        name = "fin"
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    with pytest.raises(ExtensionError, match="missing prefix"):
        registry.extensions()


def test_config_is_found_by_searching_upward(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running from a subdirectory must not change the answer.

    A tool that behaves differently depending on which directory it was
    invoked from is the single most common way two people on one repository
    get different results.
    """
    monkeypatch.delenv("VARDA_CONFIG", raising=False)
    write_config(tmp_path, 'exempt = ["V502"]')
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert registry.find_config(deep) == tmp_path / "varda.toml"


# ---------------------------------------------------------------------------
# The namespace — the one identifier that can never change
# ---------------------------------------------------------------------------


def test_profile_namespace_is_pinned() -> None:
    """The profile IRI is permanent, and this test says so out loud.

    It is copied into the `prefixes:` block of every model anyone writes and
    into every RDF graph generated from one, so changing it silently
    invalidates every model in the wild. A w3id.org identifier is used
    precisely so the redirect target can move without the identifier moving;
    if a future change wants a different target, it belongs in the w3id
    `.htaccess`, not here.

    This assertion is not testing behaviour. It is a tripwire, so that
    changing the namespace has to be deliberate.
    """
    view = registry.varda_extension().profile_view
    assert view is not None
    assert str(view.schema.id) == "https://w3id.org/varda"
    assert view.schema.prefixes["varda"].prefix_reference == (
        "https://w3id.org/varda/"
    )


def test_version_has_one_source() -> None:
    """`__version__` is the only place the version is written.

    `pyproject.toml` declares the version dynamic and reads it back out of
    `varda.__init__` via `[tool.setuptools.dynamic]`, so the number the code
    prints and the number pip reports come from the same line. This asserts
    the derivation still happens: if the `attr` lookup ever stopped
    resolving — a renamed module, a build-backend change, a stale editable
    install — the two drift apart, and every consumer sees one version in
    `pip show` and another in `varda check`.
    """
    assert importlib.metadata.version("varda") == __version__


def test_example_declares_the_same_namespace() -> None:
    """The shipped example must not drift from the profile it annotates."""
    model = DimensionalModel.load(RETAIL)
    declared = model.view.schema.prefixes["varda"].prefix_reference
    assert str(declared) == "https://w3id.org/varda/"


# ---------------------------------------------------------------------------
# Versioning columns
#
# Type 2 is defined by keeping a row per change, not by how the current row
# is identified. The field uses at least three mechanisms, and the first
# three tests exist to hold the rules open to all of them: a validator that
# only accepts the textbook one rejects working warehouses.
# ---------------------------------------------------------------------------


def test_version_window_strategy_is_accepted(tmp_path: pathlib.Path) -> None:
    """Start and end columns — the textbook form."""
    model = build(
        tmp_path,
        {
            "DimThing": versioned(
                vf=_col("VERSION_START"), vt=_col("VERSION_END")
            )
        },
    )
    assert not codes(model) & {"V503", "V504", "V505", "V506"}


def test_flagged_strategy_is_accepted(tmp_path: pathlib.Path) -> None:
    """A start and a current flag, with the end derived from the next row.

    Storing no end column is a normal design, and Data Vault virtualizes it
    outright, so its absence must not be a finding.
    """
    model = build(
        tmp_path,
        {
            "DimThing": versioned(
                vf=_col("VERSION_START"), cur=_col("IS_CURRENT", "boolean")
            )
        },
    )
    assert not codes(model) & {"V503", "V504", "V505", "V506"}


def test_version_counter_strategy_is_accepted(tmp_path: pathlib.Path) -> None:
    """A bare counter and no timestamps at all."""
    model = build(
        tmp_path,
        {"DimThing": versioned(v=_col("VERSION_NUMBER", "integer"))},
    )
    assert not codes(model) & {"V503", "V504", "V505", "V506"}


def test_v116_versioning_on_a_type_1_dimension(
    tmp_path: pathlib.Path,
) -> None:
    table = dimension()
    table["attributes"]["vf"] = _col("VERSION_START")
    model = build(tmp_path, {"DimThing": table})
    assert "V503" in codes(model)


def test_v112_versioning_on_a_fact(tmp_path: pathlib.Path) -> None:
    """A version period on a fact is caught by the role/table-kind rule."""
    fct = _fact(
        **{
            "varda:grain": ["d_key"],
            "varda:grain_statement": "one row per thing",
        }
    )
    fct["attributes"]["vf"] = _col("VERSION_START")
    model = build(tmp_path, {"FctX": fct, "DimThing": dimension()})
    assert "V103" in codes(model)


def test_v117_version_end_without_a_start(tmp_path: pathlib.Path) -> None:
    model = build(tmp_path, {"DimThing": versioned(vt=_col("VERSION_END"))})
    assert "V504" in codes(model)


def test_v118_two_columns_claim_one_versioning_role(
    tmp_path: pathlib.Path,
) -> None:
    model = build(
        tmp_path,
        {
            "DimThing": versioned(
                vf=_col("VERSION_START"), vf2=_col("VERSION_START")
            )
        },
    )
    assert "V505" in codes(model)


def test_a_type_1_dimension_is_unique_on_its_natural_key(
    tmp_path: pathlib.Path,
) -> None:
    """Type 0 and type 1 keep one row per business entity.

    V302 makes the natural key mandatory and calls it what a loader matches
    on. Only type-2 dimensions used to get a constraint, so four of the five
    dimensions in the shipped example carried an identity the database did
    not enforce.
    """
    for scd in ("TYPE_0", "TYPE_1"):
        table = dimension()
        table["annotations"]["varda:scd"] = scd
        sql = generate_sql(build(tmp_path, {"DimThing": table}))
        assert 'UNIQUE ("d_id")' in sql, scd


def test_the_natural_key_constraint_rejects_a_repeated_load(
    tmp_path: pathlib.Path,
) -> None:
    """The constraint does its job, not merely appear in the file.

    A uniqueness claim is only worth generating if the database refuses the
    second row, so this loads one and asks it to.
    """
    model = build(tmp_path, {"DimThing": dimension()})
    con = duckdb.connect()
    con.execute(generate_sql(model))
    insert = 'INSERT INTO "mart"."dim_thing" ("d_key", "d_id") VALUES'
    con.execute(f"{insert} (1, 'abc')")
    with pytest.raises(duckdb.ConstraintException):
        con.execute(f"{insert} (2, 'abc')")


def test_a_dimension_without_an_scd_gets_no_derived_key(
    tmp_path: pathlib.Path,
) -> None:
    """Silence rather than a guess, and V502 already says why.

    Emitting the natural key alone on a table that turns out to version
    would reject the second version of every row — the constraint wrong in
    the direction that looks like broken source data.
    """
    table = dimension()
    del table["annotations"]["varda:scd"]
    model = build(tmp_path, {"DimThing": table})
    assert "UNIQUE" not in generate_sql(model)
    assert "V502" in codes(model)


def test_a_type_2_marked_only_current_is_reported(
    tmp_path: pathlib.Path,
) -> None:
    """`IS_CURRENT` alone discriminates nothing.

    It is true of exactly one version, so a key carrying it lets every
    superseded row repeat. The dimension used to pass V506 and get no
    constraint, which is the same hole seen from the rule side.
    """
    model = build(
        tmp_path,
        {"DimThing": versioned(cur=_col("IS_CURRENT", "boolean"))},
    )
    assert "V506" in codes(model)
    assert "UNIQUE" not in generate_sql(model)


def test_v119_type_2_with_no_versioning_column(
    tmp_path: pathlib.Path,
) -> None:
    model = build(tmp_path, {"DimThing": versioned()})
    assert "V506" in codes(model)


def test_type_2_uniqueness_includes_the_version(
    tmp_path: pathlib.Path,
) -> None:
    """The generated constraint is the natural key plus the discriminator.

    The natural key alone would reject the second version of every row —
    wrong in the direction that looks like the source data is broken.
    """
    model = build(
        tmp_path,
        {
            "DimThing": versioned(
                vf=_col("VERSION_START"), cur=_col("IS_CURRENT", "boolean")
            )
        },
    )
    sql = generate_sql(model)
    assert 'UNIQUE ("d_id", "vf")' in sql
    assert 'UNIQUE ("d_id")' not in sql


def test_generated_docs_lead_with_the_grain_sentence(
    tmp_path: pathlib.Path,
) -> None:
    """The docs page renders the sentence, not a repr of the column tuple.

    This regressed once already: `Table.grain` changed from a string to a
    tuple, `gen_sql` was updated and `gen_docs` was not, and the page shipped
    reading `**Grain:** ('a', 'b')` with the sentence dropped entirely. No
    test looked at the line, so the whole gate stayed green.
    """
    fct = _fact(
        **{
            "varda:grain": ["d_key", "ticket"],
            "varda:grain_statement": "one row per ticket per thing",
        }
    )
    model = build(tmp_path, {"FctX": fct, "DimThing": dimension()})
    docs = generate_docs(model)
    assert "**Grain:** one row per ticket per thing" in docs
    assert "**Unique on:** `d_key`, `ticket`" in docs
    assert "('d_key'" not in docs


def test_v114_rejects_a_repeated_grain_column(
    tmp_path: pathlib.Path,
) -> None:
    fct = _fact(
        **{
            "varda:grain": ["d_key", "d_key"],
            "varda:grain_statement": "one row per thing",
        }
    )
    model = build(tmp_path, {"FctX": fct, "DimThing": dimension()})
    assert "V203" in codes(model)


def test_grain_is_checked_off_facts_too(tmp_path: pathlib.Path) -> None:
    """A grain on a bridge is validated, because it is also generated.

    `gen_sql` emits a UNIQUE for any table declaring a grain, so validating
    only facts left the one place fan-out actually happens unchecked — and
    `examples/retail.yaml` puts a grain on its bridge.
    """
    bridge = {
        "annotations": {
            "varda:role": "BRIDGE",
            "varda:grain": ["d_key", "nonesuch"],
            "varda:grain_statement": "one row per thing per other",
        },
        "attributes": {
            "d_key": {
                "range": "integer",
                "annotations": {
                    "varda:role": "FOREIGN_KEY",
                    "varda:references": "DimThing",
                },
            },
            "w": {
                "range": "decimal",
                "unit": {"symbol": "ratio"},
                "annotations": {
                    "varda:role": "MEASURE",
                    "varda:additivity": "NON_ADDITIVE",
                },
            },
        },
    }
    model = build(tmp_path, {"BridgeX": bridge, "DimThing": dimension()})
    assert "V203" in codes(model)


def test_v120_scd_on_a_fact(tmp_path: pathlib.Path) -> None:
    fct = _fact(
        **{
            "varda:grain": ["d_key"],
            "varda:grain_statement": "one row per thing",
            "varda:scd": "TYPE_2",
        }
    )
    model = build(tmp_path, {"FctX": fct, "DimThing": dimension()})
    assert "V104" in codes(model)


def test_v120_fact_type_on_a_dimension(tmp_path: pathlib.Path) -> None:
    table = dimension()
    table["annotations"]["varda:fact_type"] = "TRANSACTION"
    model = build(tmp_path, {"DimThing": table})
    assert "V104" in codes(model)


# ---------------------------------------------------------------------------
# Hierarchies — the named paths a dimension is drilled down
# ---------------------------------------------------------------------------


def _geo(*levels: str, name: str = "geography") -> dict[str, Any]:
    """Build a dimension with geography columns and one hierarchy over them."""
    table = dimension()
    for level in ("country", "region", "city"):
        table["attributes"][level] = {
            "annotations": {"varda:role": "ATTRIBUTE"}
        }
    table["annotations"]["varda:hierarchies"] = [
        {"name": name, "levels": list(levels)}
    ]
    return table


def test_a_hierarchy_reaches_the_model_as_levels(
    tmp_path: pathlib.Path,
) -> None:
    """The structured annotation crosses the typed boundary intact."""
    model = build(tmp_path, {"DimThing": _geo("country", "region", "city")})
    table = model.table("DimThing")
    assert table is not None
    (hierarchy,) = table.hierarchies
    assert hierarchy.name == "geography"
    assert hierarchy.levels == ("country", "region", "city")
    assert [c.name for c in hierarchy.level_columns] == [
        "country",
        "region",
        "city",
    ]
    assert str(hierarchy) == "DimThing.geography"
    assert not codes(model)


def test_a_column_may_sit_in_two_hierarchies(tmp_path: pathlib.Path) -> None:
    """Shared levels are the normal case, not an edge case.

    A date dimension carries a calendar path and a fiscal path over the same
    days. A design where a column belongs to one hierarchy cannot express it.
    """
    table = _geo("country", "region", "city")
    table["annotations"]["varda:hierarchies"].append(
        {"name": "sales", "levels": ["region", "city"]}
    )
    model = build(tmp_path, {"DimThing": table})
    assert not codes(model)
    found = model.table("DimThing")
    assert found is not None
    assert [h.name for h in found.hierarchies] == ["geography", "sales"]


def test_v121_level_is_not_a_column(tmp_path: pathlib.Path) -> None:
    model = build(tmp_path, {"DimThing": _geo("country", "nosuch")})
    assert "V601" in codes(model)


def test_v122_level_appears_twice(tmp_path: pathlib.Path) -> None:
    model = build(tmp_path, {"DimThing": _geo("country", "region", "region")})
    assert "V602" in codes(model)


def test_v123_one_level_is_not_a_hierarchy(tmp_path: pathlib.Path) -> None:
    model = build(tmp_path, {"DimThing": _geo("country")})
    assert "V603" in codes(model)


def test_v124_two_hierarchies_share_a_name(tmp_path: pathlib.Path) -> None:
    table = _geo("country", "region")
    table["annotations"]["varda:hierarchies"].append(
        {"name": "geography", "levels": ["country", "city"]}
    )
    model = build(tmp_path, {"DimThing": table})
    assert "V604" in codes(model)


def test_v124_a_hierarchy_without_a_name(tmp_path: pathlib.Path) -> None:
    table = _geo("country", "region")
    table["annotations"]["varda:hierarchies"] = [
        {"levels": ["country", "region"]}
    ]
    model = build(tmp_path, {"DimThing": table})
    assert "V604" in codes(model)


@pytest.mark.parametrize(
    "role", ["SURROGATE_KEY", "MEASURE", "VERSION_START", "IS_CURRENT"]
)
def test_v125_level_role_cannot_roll_up(
    tmp_path: pathlib.Path, role: str
) -> None:
    table = _geo("country", "odd")
    table["attributes"]["odd"] = {
        "range": "decimal" if role == "MEASURE" else "string",
        "annotations": {"varda:role": role},
    }
    if role == "MEASURE":
        table["attributes"]["odd"]["annotations"]["varda:additivity"] = (
            "ADDITIVE"
        )
    model = build(tmp_path, {"DimThing": table})
    assert "V605" in codes(model)


def test_v125_a_natural_key_is_a_legal_leaf(tmp_path: pathlib.Path) -> None:
    """The finest level of a dimension's path is usually its natural key."""
    model = build(tmp_path, {"DimThing": _geo("country", "region", "d_id")})
    assert "V605" not in codes(model)


def _snowflake(levels: list[str | dict[str, str]]) -> dict[str, Any]:
    """Build a city/state/country snowflake with one hierarchy over it.

    The coarser levels are their own tables, which is the arrangement that
    makes the reference form necessary: the only geography columns on
    DimCity are the keys.
    """
    country = dimension()
    country["attributes"]["country_name"] = {
        "annotations": {"varda:role": "ATTRIBUTE"}
    }
    state = dimension()
    state["attributes"]["state_name"] = {
        "annotations": {"varda:role": "ATTRIBUTE"}
    }
    city = dimension()
    city["annotations"]["varda:hierarchies"] = [
        {"name": "geography", "levels": levels}
    ]
    city["attributes"]["city_name"] = {
        "annotations": {"varda:role": "ATTRIBUTE"}
    }
    for name, target in (
        ("state_key", "DimState"),
        ("country_key", "DimCountry"),
    ):
        city["attributes"][name] = {
            "range": "integer",
            "annotations": {
                "varda:role": "FOREIGN_KEY",
                "varda:references": target,
            },
        }
    return {"DimCity": city, "DimState": state, "DimCountry": country}


def test_a_level_reaches_through_a_foreign_key(
    tmp_path: pathlib.Path,
) -> None:
    """A snowflaked level resolves to a column of the table it points at."""
    model = build(
        tmp_path,
        _snowflake(
            ["country_key.country_name", "state_key.state_name", "city_name"]
        ),
    )
    assert not codes(model)
    table = model.table("DimCity")
    assert table is not None
    (hierarchy,) = table.hierarchies
    country, state, city = hierarchy.resolved
    assert country.is_reference
    assert country.via is not None
    assert country.via.name == "country_key"
    assert country.column is not None
    assert country.column.name == "country_name"
    assert not city.is_reference
    assert city.via is None
    assert state.column is not None


def test_v125_a_bare_foreign_key_is_not_a_level(
    tmp_path: pathlib.Path,
) -> None:
    """The near miss a snowflake invites: a path of unreadable integers."""
    model = build(
        tmp_path, _snowflake(["country_key", "state_key", "city_name"])
    )
    found = [f for f in rules.check(model) if f.rule == "V605"]
    assert found
    # The message has to carry the fix, since the right answer is one dot away.
    assert "country_key.<column of DimCountry>" in found[0].message


@pytest.mark.parametrize(
    ("level", "fragment"),
    [
        ("nosuch.country_name", "not a column of DimCity"),
        ("d_key.country_name", "which is a SURROGATE_KEY"),
        ("country_key.nosuch", "not a column of DimCountry"),
    ],
)
def test_v121_a_reference_level_that_does_not_resolve(
    tmp_path: pathlib.Path, level: str, fragment: str
) -> None:
    """Each way a reference can fail names the half that is wrong."""
    model = build(
        tmp_path, _snowflake([level, "state_key.state_name", "city_name"])
    )
    found = [f for f in rules.check(model) if f.rule == "V601"]
    assert found
    assert fragment in found[0].message


def test_a_hierarchy_can_say_what_it_is_for(tmp_path: pathlib.Path) -> None:
    """A dimension with several paths needs more than terse names."""
    table = _geo("country", "region", "city")
    table["annotations"]["varda:hierarchies"][0]["description"] = (
        "How stores roll up for sales reporting."
    )
    model = build(tmp_path, {"DimThing": table})
    assert not codes(model)
    found = model.table("DimThing")
    assert found is not None
    assert found.hierarchies[0].description == (
        "How stores roll up for sales reporting."
    )
    page = generate_docs(model)
    assert "— How stores roll up for sales reporting." in page


def test_a_hierarchy_without_a_description_renders_plainly(
    tmp_path: pathlib.Path,
) -> None:
    """The dash only appears when there is something after it."""
    model = build(tmp_path, {"DimThing": _geo("country", "region", "city")})
    page = generate_docs(model)
    assert "**Drill path** (geography): `country` → `region` → `city`\n" in page


def test_docs_render_a_reference_level_qualified(
    tmp_path: pathlib.Path,
) -> None:
    """`country_name` alone would send a reader to the wrong table."""
    model = build(
        tmp_path,
        _snowflake(
            ["country_key.country_name", "state_key.state_name", "city_name"]
        ),
    )
    page = generate_docs(model)
    assert (
        "**Drill path** (geography): `DimCountry.country_name` → "
        "`DimState.state_name` → `city_name`" in page
    )


def test_a_level_key_defaults_to_what_identifies_it(
    tmp_path: pathlib.Path,
) -> None:
    """A plain level is keyed on its column, a reference on its foreign key."""
    model = build(
        tmp_path,
        _snowflake(
            ["country_key.country_name", "state_key.state_name", "city_name"]
        ),
    )
    table = model.table("DimCity")
    assert table is not None
    country, state, city = table.hierarchies[0].resolved
    assert country.key is not None
    assert country.key.name == "country_key"
    assert city.key is not None
    assert city.key.name == "city_name"
    assert not country.declared_key
    # Identity is the key of every coarser level, then this one.
    assert [c.name for c in country.identity] == ["country_key"]
    assert [c.name for c in state.identity] == ["country_key", "state_key"]
    assert [c.name for c in city.identity] == [
        "country_key",
        "state_key",
        "city_name",
    ]


def test_a_denormalized_level_needs_no_key(tmp_path: pathlib.Path) -> None:
    """`city_name` names a member without identifying one, and that is fine.

    Springfield exists in three states, so the level is identified by the
    columns above it as well — which the hierarchy already states. Nothing
    has to be declared.
    """
    table = dimension()
    for level in ("country_name", "state_name", "city_name"):
        table["attributes"][level] = {
            "annotations": {"varda:role": "ATTRIBUTE"}
        }
    table["annotations"]["varda:hierarchies"] = [
        {
            "name": "geography",
            "levels": ["country_name", "state_name", "city_name"],
        }
    ]
    model = build(tmp_path, {"DimThing": table})
    assert not codes(model)
    found = model.table("DimThing")
    assert found is not None
    levels = found.hierarchies[0].resolved
    assert [c.name for c in levels[2].identity] == [
        "country_name",
        "state_name",
        "city_name",
    ]


def test_a_level_can_be_keyed_on_another_column(
    tmp_path: pathlib.Path,
) -> None:
    """A level shown as `product_name` where `sku` is what tells them apart."""
    table = dimension()
    for name in ("brand", "product_name", "sku"):
        table["attributes"][name] = {"annotations": {"varda:role": "ATTRIBUTE"}}
    table["annotations"]["varda:hierarchies"] = [
        {
            "name": "merchandise",
            "levels": ["brand", {"column": "product_name", "key": "sku"}],
        }
    ]
    model = build(tmp_path, {"DimThing": table})
    assert not codes(model)
    found = model.table("DimThing")
    assert found is not None
    product = found.hierarchies[0].resolved[1]
    assert product.column is not None
    assert product.column.name == "product_name"
    assert product.key is not None
    assert product.key.name == "sku"
    assert [c.name for c in product.identity] == ["brand", "sku"]


def _keyed(key: str) -> dict[str, Any]:
    """Build a dimension whose city level declares the given key."""
    table = dimension()
    table["attributes"]["city_name"] = {
        "annotations": {"varda:role": "ATTRIBUTE"}
    }
    table["attributes"]["floor_m2"] = {
        "range": "decimal",
        "annotations": {
            "varda:role": "MEASURE",
            "varda:additivity": "ADDITIVE",
        },
    }
    table["annotations"]["varda:hierarchies"] = [
        {
            "name": "geography",
            "levels": ["d_id", {"column": "city_name", "key": key}],
        }
    ]
    return table


@pytest.mark.parametrize(
    ("key", "fragment"),
    [
        ("nosuch", "not a column of DimThing"),
        ("floor_m2", "is a MEASURE and identifies no member"),
    ],
)
def test_v127_a_key_that_identifies_nothing(
    tmp_path: pathlib.Path, key: str, fragment: str
) -> None:
    model = build(tmp_path, {"DimThing": _keyed(key)})
    found = [f for f in rules.check(model) if f.rule == "V607"]
    assert found
    assert fragment in found[0].message


def test_a_reference_level_is_keyed_in_the_table_it_names(
    tmp_path: pathlib.Path,
) -> None:
    """`column` and `key` describe one level, so they resolve together.

    A level written `country_key.country_name` is named by DimCountry, so a
    key declared for it is a column of DimCountry. Looking in the near table
    reported `country_code` missing from DimCity, where it was never meant
    to be.
    """
    classes = _snowflake(
        [
            {"column": "country_key.country_name", "key": "country_code"},
            "city_name",
        ]
    )
    # Only DimCountry has it, so resolving in DimCity cannot pass by luck.
    classes["DimCountry"]["attributes"]["country_code"] = {
        "annotations": {"varda:role": "ATTRIBUTE"}
    }
    model = build(tmp_path, classes)
    assert "V607" not in codes(model)


def test_a_missing_reference_key_names_the_far_table(
    tmp_path: pathlib.Path,
) -> None:
    """The message sends a reader to the table the key belongs in."""
    model = build(
        tmp_path,
        _snowflake(
            [
                {"column": "country_key.country_name", "key": "nonsense"},
                "city_name",
            ]
        ),
    )
    found = [f for f in rules.check(model) if f.rule == "V607"]
    assert found
    assert "not a column of DimCountry" in found[0].message


# ---------------------------------------------------------------------------
# Bridges, and the annotations that carry structure
# ---------------------------------------------------------------------------


def test_v133_a_bridge_that_references_nothing(
    tmp_path: pathlib.Path,
) -> None:
    bridge = {
        "annotations": {"varda:role": "BRIDGE"},
        "attributes": {
            "weight": {
                "range": "decimal",
                "annotations": {
                    "varda:role": "MEASURE",
                    "varda:additivity": "ADDITIVE",
                },
            }
        },
    }
    model = build(tmp_path, {"BridgeLonely": bridge})
    assert "V405" in codes(model)


def test_v133_accepts_a_single_group_key(tmp_path: pathlib.Path) -> None:
    """Kimball's group-key bridge carries one foreign key, not two.

    The fact points at a group and the bridge maps that group to a
    dimension, so demanding a pair would refuse a standard design.
    """
    bridge = {
        "annotations": {"varda:role": "BRIDGE"},
        "attributes": {
            "group_key": {
                "range": "integer",
                "annotations": {"varda:role": "DEGENERATE_DIMENSION"},
            },
            "d_key": {
                "range": "integer",
                "annotations": {
                    "varda:role": "FOREIGN_KEY",
                    "varda:references": "DimThing",
                },
            },
        },
    }
    model = build(tmp_path, {"BridgeGroup": bridge, "DimThing": dimension()})
    assert "V405" not in codes(model)


def test_v134_a_typo_in_a_structured_annotation(
    tmp_path: pathlib.Path,
) -> None:
    """`levles` was reported as a hierarchy with zero levels."""
    table = dimension()
    table["attributes"]["cat"] = {"annotations": {"varda:role": "ATTRIBUTE"}}
    table["annotations"]["varda:hierarchies"] = [
        {"name": "h", "levles": ["cat", "d_id"]}
    ]
    model = build(tmp_path, {"DimThing": table})
    found = [f for f in rules.check(model) if f.rule == "V004"]
    assert found
    assert "'levles'" in found[0].message
    assert "levels" in found[0].message


def test_v134_reaches_a_typo_inside_a_level(
    tmp_path: pathlib.Path,
) -> None:
    """`colunm` was reported as ``level '' is not a column``."""
    table = dimension()
    table["attributes"]["cat"] = {"annotations": {"varda:role": "ATTRIBUTE"}}
    table["annotations"]["varda:hierarchies"] = [
        {"name": "h", "levels": [{"colunm": "cat", "key": "d_id"}, "d_id"]}
    ]
    model = build(tmp_path, {"DimThing": table})
    found = [f for f in rules.check(model) if f.rule == "V004"]
    assert found
    assert "varda:hierarchies.levels" in found[0].message
    assert "'colunm'" in found[0].message


def test_v134_is_silent_on_a_well_formed_hierarchy(
    tmp_path: pathlib.Path,
) -> None:
    """A bare string level is not a mapping and has no fields to check."""
    table = dimension()
    table["attributes"]["cat"] = {"annotations": {"varda:role": "ATTRIBUTE"}}
    table["annotations"]["varda:hierarchies"] = [
        {"name": "h", "description": "why", "levels": ["cat", "d_id"]}
    ]
    model = build(tmp_path, {"DimThing": table})
    assert "V004" not in codes(model)


# ---------------------------------------------------------------------------
# Identity — what makes two rows the same thing
# ---------------------------------------------------------------------------


# --- several natural keys ----------------------------------------------------


def _two_identities(**extra: Any) -> dict[str, Any]:
    """Build a dimension a product is identified two different ways in."""
    table = dimension()
    table["attributes"] = {
        "d_key": table["attributes"]["d_key"],
        "gtin": {"annotations": {"varda:role": "NATURAL_KEY"}},
        "supplier_part": {"annotations": {"varda:role": "NATURAL_KEY"}},
    }
    table.update(extra)
    return table


def test_v306_several_natural_keys_and_nothing_said(
    tmp_path: pathlib.Path,
) -> None:
    model = build(tmp_path, {"DimThing": _two_identities()})
    found = [f for f in rules.check(model) if f.rule == "V306"]
    assert len(found) == 1
    assert "gtin, supplier_part" in found[0].message


def test_a_merged_constraint_is_no_longer_derived(
    tmp_path: pathlib.Path,
) -> None:
    """The constraint this replaces, and why nothing takes its place.

    `UNIQUE (gtin, supplier_part)` was emitted for years and enforced
    neither identity: two rows may share a barcode as long as their part
    numbers differ, and a NULL on either side — the normal state of a table
    whose two sources each fill one column — leaves the row unconstrained
    outright. Emitting nothing is worse than a correct constraint and better
    than that one, and V306 is what stops it being the end of the story.
    """
    model = build(tmp_path, {"DimThing": _two_identities()})
    sql = generate_sql(model)
    assert "UNIQUE" not in sql
    assert "V306" in codes(model)


def test_the_merged_constraint_enforced_neither_identity() -> None:
    """Pinned against a real engine, because the claim is about behavior.

    Built by hand rather than generated — the generator no longer emits this
    — so that the reason V306 is an error stays visible after the shape that
    produced it is gone.
    """
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE t (gtin VARCHAR, part VARCHAR, UNIQUE (gtin, part))"
    )
    conn.execute("INSERT INTO t VALUES ('0012345678905', 'A')")
    conn.execute("INSERT INTO t VALUES ('0012345678905', 'B')")
    conn.execute("INSERT INTO t VALUES ('0012345678905', NULL)")
    conn.execute("INSERT INTO t VALUES ('0012345678905', NULL)")
    shared = conn.execute(
        "SELECT count(*) FROM t WHERE gtin = '0012345678905'"
    ).fetchone()
    assert shared == (4,)


def test_declared_keys_settle_it(tmp_path: pathlib.Path) -> None:
    """Two alternative identities become two independent constraints."""
    table = _two_identities(
        unique_keys={
            "by_barcode": {"unique_key_slots": ["gtin"]},
            "by_supplier": {"unique_key_slots": ["supplier_part"]},
        }
    )
    model = build(tmp_path, {"DimThing": table})
    sql = generate_sql(model)
    assert 'UNIQUE ("gtin")' in sql
    assert 'UNIQUE ("supplier_part")' in sql
    assert "V306" not in codes(model)


def test_one_compound_identity_is_declared_the_same_way(
    tmp_path: pathlib.Path,
) -> None:
    """The other reading, said out loud rather than guessed at.

    A store known by its chain and its number wants exactly the merged
    constraint V306 refuses to derive. Nothing stops it — the model just has
    to be the one that says so.
    """
    table = _two_identities(
        unique_keys={
            "business": {"unique_key_slots": ["gtin", "supplier_part"]}
        }
    )
    model = build(tmp_path, {"DimThing": table})
    assert 'UNIQUE ("gtin", "supplier_part")' in generate_sql(model)
    assert "V306" not in codes(model)


def test_one_natural_key_still_derives(tmp_path: pathlib.Path) -> None:
    """The unambiguous case is unchanged: one identity, one constraint."""
    model = build(tmp_path, {"DimThing": dimension()})
    assert 'UNIQUE ("d_id")' in generate_sql(model)
    assert "V306" not in codes(model)


def test_v307_a_lone_identity_that_may_be_absent(
    tmp_path: pathlib.Path,
) -> None:
    table = dimension()
    del table["attributes"]["d_id"]["required"]
    model = build(tmp_path, {"DimThing": table})
    found = [f for f in rules.check(model) if f.rule == "V307"]
    assert len(found) == 1
    assert found[0].severity == "warning"


def test_v307_leaves_alternative_identities_alone(
    tmp_path: pathlib.Path,
) -> None:
    """Absence is the point where a dimension has several identities.

    A product read from a barcode feed has no supplier part number and one
    read from a supplier catalog has no barcode. Warning on those would fire
    on every well-formed table of the shape and be switched off.
    """
    table = _two_identities(
        unique_keys={
            "by_barcode": {"unique_key_slots": ["gtin"]},
            "by_supplier": {"unique_key_slots": ["supplier_part"]},
        }
    )
    model = build(tmp_path, {"DimThing": table})
    assert "V307" not in codes(model)


def test_a_nullable_lone_key_does_not_hold(tmp_path: pathlib.Path) -> None:
    """Why V307 exists: the constraint is there and does not bite."""
    table = dimension()
    del table["attributes"]["d_id"]["required"]
    model = build(tmp_path, {"DimThing": table})
    conn = duckdb.connect()
    conn.execute(generate_sql(model))
    for key in (1, 2, 3):
        conn.execute("INSERT INTO mart.dim_thing VALUES (NULL, ?)", [key])
    kept = conn.execute("SELECT count(*) FROM mart.dim_thing").fetchone()
    assert kept == (3,)


# --- declared unique keys ----------------------------------------------------


def _two_keyed(**extra: Any) -> dict[str, Any]:
    """Build a type-2 dimension identified two ways by two sources."""
    table = versioned(vs=_col("VERSION_START"))
    table["attributes"]["gtin"] = {"annotations": {"varda:role": "NATURAL_KEY"}}
    table["attributes"]["supplier_code"] = {
        "annotations": {"varda:role": "NATURAL_KEY"}
    }
    table["unique_keys"] = {
        "by_barcode": {"unique_key_slots": ["gtin", "vs"]},
        "by_supplier": {"unique_key_slots": ["supplier_code", "vs"]},
    }
    table.update(extra)
    return table


def test_declared_unique_keys_become_separate_constraints(
    tmp_path: pathlib.Path,
) -> None:
    """Two keys are two constraints, never one merged key.

    Merging them is weaker than either alone, and inert besides: a NULL on
    one side of a merged key leaves the whole row unconstrained.
    """
    table = _two_keyed()
    table["attributes"]["d_id"]["annotations"] = {"varda:role": "ATTRIBUTE"}
    model = build(tmp_path, {"DimProduct": table})
    sql = generate_sql(model)
    assert 'UNIQUE ("gtin", "vs")' in sql
    assert 'UNIQUE ("supplier_code", "vs")' in sql
    # and not the concatenation of everything
    assert 'UNIQUE ("gtin", "supplier_code' not in sql


def test_declared_keys_replace_the_derived_one(
    tmp_path: pathlib.Path,
) -> None:
    """A table states its uniqueness in one place or the other, never both."""
    model = build(tmp_path, {"DimProduct": _two_keyed()})
    sql = generate_sql(model)
    # `d_id` is the natural key Varda would otherwise have derived from.
    assert 'UNIQUE ("d_id", "vs")' not in sql


def _fact_keyed_twice() -> dict[str, Any]:
    """Build a fact whose grain and unique key name the same column."""
    return {
        "annotations": {
            "varda:role": "FACT",
            # Factless, so the fixture is legal without a measure that has
            # nothing to do with what is being tested.
            "varda:fact_type": "FACTLESS",
            "varda:grain": "d_key",
            "varda:grain_statement": "one row per thing measured once",
        },
        "unique_keys": {"by_thing": {"unique_key_slots": ["d_key"]}},
        "attributes": {
            "d_key": {
                "range": "integer",
                "annotations": {
                    "varda:role": "FOREIGN_KEY",
                    "varda:references": "DimThing",
                },
            }
        },
    }


def test_one_claim_is_one_constraint(tmp_path: pathlib.Path) -> None:
    """A grain and a matching unique key are one claim written twice.

    One is Varda's vocabulary and one is LinkML's, and writing both is a
    reasonable thing to do. Every database accepts the doubled constraint by
    building a second index for it — maintained on every write, for a claim
    it already holds, with nothing anywhere saying so.
    """
    model = build(
        tmp_path, {"DimThing": dimension(), "FctThing": _fact_keyed_twice()}
    )
    assert not codes(model)
    sql = generate_sql(model)
    assert sql.count('UNIQUE ("d_key")') == 1


def test_one_claim_is_one_index(tmp_path: pathlib.Path) -> None:
    """The form the reader of the database sees, rather than of the DDL."""
    model = build(
        tmp_path, {"DimThing": dimension(), "FctThing": _fact_keyed_twice()}
    )
    con = duckdb.connect()
    con.execute(generate_sql(model, dialect_name="duckdb"))
    found = con.execute(
        "select count(*) from duckdb_constraints() "
        "where table_name = 'fct_thing' and constraint_type = 'UNIQUE'"
    ).fetchone()
    assert found is not None
    assert found[0] == 1


def test_two_different_claims_are_still_two_constraints(
    tmp_path: pathlib.Path,
) -> None:
    """De-duplication is on the columns, not on there being more than one."""
    model = build(tmp_path, {"DimProduct": _two_keyed()})
    sql = generate_sql(model)
    assert sql.count("UNIQUE (") == 2


def test_unique_keys_are_inherited(tmp_path: pathlib.Path) -> None:
    """LinkML drops a parent's unique keys; Varda walks the ancestors.

    Columns arrive through `class_induced_slots`, which inherits. A table
    that inherits its columns and silently loses the constraint over them is
    a disagreement nobody can debug.
    """
    model = build(
        tmp_path,
        {
            "Auditable": {
                "unique_keys": {
                    "by_source_ref": {"unique_key_slots": ["source_ref"]}
                },
                "attributes": {
                    "source_ref": {"annotations": {"varda:role": "ATTRIBUTE"}}
                },
            },
            "DimThing": {**dimension(), "is_a": "Auditable"},
        },
    )
    table = model.table("DimThing")
    assert table is not None
    assert [u.name for u in table.unique_keys] == ["by_source_ref"]
    assert 'UNIQUE ("source_ref")' in generate_sql(model)


def test_v128_unique_key_names_nothing(tmp_path: pathlib.Path) -> None:
    """LinkML accepts a key over a misspelled slot without complaint."""
    table = dimension()
    table["unique_keys"] = {"k": {"unique_key_slots": ["d_idd"]}}
    model = build(tmp_path, {"DimThing": table})
    found = [f for f in rules.check(model) if f.rule == "V303"]
    assert found
    assert "d_idd" in found[0].message


def test_v129_business_key_without_a_version(tmp_path: pathlib.Path) -> None:
    """A business key on a type-2 dimension repeats once per version."""
    table = versioned(vs=_col("VERSION_START"))
    table["unique_keys"] = {"by_id": {"unique_key_slots": ["d_id"]}}
    model = build(tmp_path, {"DimThing": table})
    assert "V304" in codes(model)


def test_v129_ignores_a_key_that_is_already_unique(
    tmp_path: pathlib.Path,
) -> None:
    """A key over the surrogate key needs no version marker added."""
    table = versioned(vs=_col("VERSION_START"))
    table["unique_keys"] = {"by_key": {"unique_key_slots": ["d_key"]}}
    model = build(tmp_path, {"DimThing": table})
    assert "V304" not in codes(model)


def test_v130_a_natural_key_no_unique_key_covers(
    tmp_path: pathlib.Path,
) -> None:
    """Declared keys replace the derived one, so an uncovered key is loose."""
    table = versioned(vs=_col("VERSION_START"))
    table["attributes"]["gtin"] = {"annotations": {"varda:role": "NATURAL_KEY"}}
    table["unique_keys"] = {
        "by_id": {"unique_key_slots": ["d_id", "vs"]},
    }
    model = build(tmp_path, {"DimThing": table})
    found = [f for f in rules.check(model) if f.rule == "V305"]
    assert found
    assert "gtin" in found[0].subject


def test_v130_is_silent_without_declared_keys(
    tmp_path: pathlib.Path,
) -> None:
    """Without them Varda derives the constraint and the question is moot."""
    model = build(tmp_path, {"DimThing": versioned(vs=_col("VERSION_START"))})
    assert "V305" not in codes(model)


# ---------------------------------------------------------------------------
# Physical names — the collisions no rule used to see
# ---------------------------------------------------------------------------


def test_v131_two_classes_claiming_one_table_name(
    tmp_path: pathlib.Path,
) -> None:
    left, right = dimension(), dimension()
    left["annotations"]["varda:physical_name"] = "dim_shared"
    right["annotations"]["varda:physical_name"] = "dim_shared"
    model = build(tmp_path, {"DimLeft": left, "DimRight": right})
    assert "V801" in codes(model)


def test_v131_catches_a_collision_nobody_declared(
    tmp_path: pathlib.Path,
) -> None:
    """`DimCustomer` and `Dim_Customer` both derive `dim_customer`.

    The message says which side came from an annotation and which from the
    class name, because "duplicate" alone sends a reader looking for a
    `varda:physical_name` that is not there.
    """
    model = build(
        tmp_path,
        {"DimCustomer": dimension(), "Dim_Customer": dimension()},
    )
    found = [f for f in rules.check(model) if f.rule == "V801"]
    assert len(found) == 1
    assert "derived" in found[0].message
    assert "varda:physical_name" not in found[0].message


def test_v131_catches_names_that_differ_only_in_case(
    tmp_path: pathlib.Path,
) -> None:
    """Quoting does not settle case, and engines disagree about it.

    PostgreSQL treats `"Foo"` and `"foo"` as two tables; DuckDB refuses the
    second as a duplicate. A model is checked once and generated for any
    dialect, so the pair is refused there rather than left to whichever
    database sees it first. The message says that is what happened, because
    two names that look different are the confusing case to be told about.
    """
    left, right = dimension(), dimension()
    left["annotations"]["varda:physical_name"] = "DimThing"
    right["annotations"]["varda:physical_name"] = "dimthing"
    model = build(tmp_path, {"DimLeft": left, "DimRight": right})
    found = [f for f in rules.check(model) if f.rule == "V801"]
    assert len(found) == 1
    assert "differ only in case" in found[0].message
    with pytest.raises(duckdb.Error):
        duckdb.connect().execute(generate_sql(model))


def test_v132_two_columns_claiming_one_name(tmp_path: pathlib.Path) -> None:
    table = dimension()
    for slot in ("name_one", "name_two"):
        table["attributes"][slot] = {
            "annotations": {
                "varda:role": "ATTRIBUTE",
                "varda:physical_name": "label",
            }
        }
    model = build(tmp_path, {"DimThing": table})
    assert "V802" in codes(model)


def test_v132_sees_an_inherited_column(tmp_path: pathlib.Path) -> None:
    """A slot from a parent collides exactly as a local one would.

    Columns are read through `class_induced_slots`, so the emitted table
    carries both — and a rule reading only `attributes` would miss it.
    """
    child = dimension()
    child["is_a"] = "Base"
    child["attributes"]["local"] = {
        "annotations": {
            "varda:role": "ATTRIBUTE",
            "varda:physical_name": "label",
        }
    }
    base = {
        "attributes": {
            "inherited": {
                "annotations": {
                    "varda:role": "ATTRIBUTE",
                    "varda:physical_name": "label",
                }
            }
        }
    }
    model = build(tmp_path, {"Base": base, "DimThing": child})
    assert "V802" in codes(model)


def test_a_column_collision_would_not_execute(
    tmp_path: pathlib.Path,
) -> None:
    """Why V802 is an error and not a warning.

    Nothing about the model is ambiguous — the DDL is simply illegal, and
    before these rules existed `varda generate` produced it and exited 0.
    """
    table = dimension()
    for slot in ("name_one", "name_two"):
        table["attributes"][slot] = {
            "annotations": {
                "varda:role": "ATTRIBUTE",
                "varda:physical_name": "label",
            }
        }
    sql = generate_sql(build(tmp_path, {"DimThing": table}))
    with pytest.raises(duckdb.Error):
        duckdb.connect().execute(sql)


# ---------------------------------------------------------------------------
# Dialects — the spellings the one model comes out in
# ---------------------------------------------------------------------------

#: sqlglot's name for each dialect Varda ships.
SQLGLOT_NAMES = {
    "postgres": "postgres",
    "duckdb": "duckdb",
    "snowflake": "snowflake",
    "sqlserver": "tsql",
}

#: Spellings of one type that sqlglot prefers and Varda does not. Every pair
#: here is the same type under two names that the engine accepts either of —
#: DuckDB takes `VARCHAR` and `TEXT`, PostgreSQL takes `INTEGER` and `INT` —
#: so normalizing through this compares what the column will be rather than
#: how two tools chose to spell it.
TYPE_ALIASES = {
    "INT": "INTEGER",
    "DECIMAL": "NUMERIC",
    "TEXT": "VARCHAR",
    "DOUBLE": "DOUBLE PRECISION",
}


def _sqlglot_type(base: str, write: str) -> str:
    """Ask sqlglot what PostgreSQL's `base` is called in `write`."""
    tree = sqlglot.parse_one(f'CREATE TABLE t ("c" {base})', read="postgres")
    column = tree.find(sqlglot_exp.ColumnDef)
    assert column is not None
    kind = column.kind
    assert kind is not None
    return str(kind.sql(dialect=write)).upper()


@pytest.mark.parametrize("name", sorted(gen_sql.DIALECTS))
def test_the_dialect_table_agrees_with_sqlglot(name: str) -> None:
    """Every type Varda emits is the one sqlglot emits for that engine.

    DuckDB is the only engine this suite can execute, so the other tables
    would be lookups nobody has ever run — which is exactly how `TIMESTAMP`
    came to be emitted for SQL Server, where it names a row-version counter
    with no date in it. sqlglot maintains the same mapping as its whole
    purpose, so the tables are checked against something rather than against
    nothing.

    Disagreement here does not automatically mean Varda is wrong. It means
    one of the two moved, and that a person has to look.
    """
    dialect = gen_sql.DIALECTS[name]
    for rng, base in gen_sql.TYPES.items():
        ours = dialect.type_of(rng)
        assert ours is not None
        theirs = _sqlglot_type(base, SQLGLOT_NAMES[name])
        assert TYPE_ALIASES.get(ours, ours) == TYPE_ALIASES.get(
            theirs, theirs
        ), f"{name}: {rng} is {ours} here and {theirs} in sqlglot"


@pytest.mark.parametrize("name", sorted(gen_sql.DIALECTS))
def test_a_schema_name_with_a_quote_in_it_still_parses(name: str) -> None:
    """`quoted` settled identifiers and nothing settled string literals.

    T-SQL cannot guard `CREATE SCHEMA` with an `IF`, so it tests
    `SCHEMA_ID('...')` and runs the DDL through `EXEC('...')` — two string
    literals holding a name that arrives from `--schema` unexamined. An
    apostrophe in it emitted DDL no database would parse, and only under the
    one dialect this suite cannot execute.
    """
    model = DimensionalModel.load(RETAIL)
    ddl = generate_sql(model, schema="ma'rt", dialect_name=name)
    statement = ddl.splitlines()[4]
    assert "ma'rt" in ddl
    sqlglot.parse(statement, read=SQLGLOT_NAMES[name])


def test_the_schema_statement_escapes_both_of_its_layers() -> None:
    """The T-SQL form quotes the name twice over, at two different depths."""
    model = DimensionalModel.load(RETAIL)
    ddl = generate_sql(model, schema="ma'rt", dialect_name="sqlserver")
    assert "SCHEMA_ID('ma''rt')" in ddl
    assert """EXEC('CREATE SCHEMA "ma''rt"')""" in ddl


def test_the_default_dialect_is_postgres() -> None:
    """The base table is PostgreSQL's, and says so.

    Naming it is the substance of this change rather than a label on it: the
    output was called dialect-neutral while emitting `BOOLEAN`, which SQL
    Server does not have, and `TIMESTAMP`, which SQL Server has and means
    something else by.
    """
    assert gen_sql.DEFAULT_DIALECT == "postgres"
    assert gen_sql.DIALECTS["postgres"].types == {}


def test_the_context_default_matches_the_generator_default() -> None:
    """Two copies of one string, and the reason for them is good.

    `Context.dialect` is a name rather than a table so that `ext.py` — the
    only module a third party imports — stays free of SQL. That is worth
    keeping, and it leaves the default written down twice. `ONE_IDENTITY`
    exists on the argument that two copies of a threshold drift, and the
    version is single-sourced on the same one; this is the copy that was
    left ungated.
    """
    assert (
        Context(
            model=DimensionalModel.load(RETAIL, importmap=registry.importmap()),
            source=RETAIL,
        ).dialect
        == gen_sql.DEFAULT_DIALECT
    )


def test_an_unknown_dialect_names_the_known_ones() -> None:
    with pytest.raises(gen_sql.GenerationError) as caught:
        gen_sql.dialect("orcale")
    assert "sqlserver" in str(caught.value)


def test_sqlserver_fixes_the_versioning_columns(
    tmp_path: pathlib.Path,
) -> None:
    """The bug that motivates the whole dialect.

    `TIMESTAMP` is a synonym for `rowversion` in T-SQL — an incrementing
    counter with no date in it. Emitted for `valid_from`, it produces a
    type-2 dimension whose version period cannot hold a version period,
    on a table every V5xx rule has just certified.
    """
    table = versioned(vs=_col("VERSION_START"))
    table["attributes"]["d_id"]["annotations"]["varda:max_length"] = 20
    model = build(tmp_path, {"DimThing": table})
    assert '"vs" TIMESTAMP' in generate_sql(model)
    tsql = generate_sql(model, dialect_name="sqlserver")
    assert '"vs" DATETIME2' in tsql
    assert "TIMESTAMP" not in tsql


def test_sqlserver_creates_its_schema_its_own_way(
    tmp_path: pathlib.Path,
) -> None:
    """`CREATE SCHEMA` must lead its batch, so it cannot be guarded by IF."""
    table = dimension()
    table["attributes"]["d_id"]["annotations"]["varda:max_length"] = 20
    model = build(tmp_path, {"DimThing": table})
    tsql = generate_sql(model, dialect_name="sqlserver")
    assert "IF SCHEMA_ID('mart') IS NULL EXEC(" in tsql
    assert "CREATE SCHEMA IF NOT EXISTS" not in tsql


def test_sqlserver_refuses_a_string_with_no_width(
    tmp_path: pathlib.Path,
) -> None:
    """A bare `VARCHAR` is a `VARCHAR(1)` there, and truncates in silence.

    Refused rather than widened to `VARCHAR(MAX)`, which cannot be a key
    column: a natural key emitted that way takes the whole file down at the
    UNIQUE constraint, having looked fine.
    """
    model = build(tmp_path, {"DimThing": dimension()})
    generate_sql(model)  # fine on postgres
    with pytest.raises(gen_sql.GenerationError) as caught:
        generate_sql(model, dialect_name="sqlserver")
    assert "varda:max_length" in str(caught.value)


def test_a_width_satisfies_sqlserver(tmp_path: pathlib.Path) -> None:
    table = dimension()
    table["attributes"]["d_id"]["annotations"]["varda:max_length"] = 20
    model = build(tmp_path, {"DimThing": table})
    assert '"d_id" VARCHAR(20)' in generate_sql(model, dialect_name="sqlserver")


def test_the_header_names_the_dialect(tmp_path: pathlib.Path) -> None:
    """A file written for one engine has to say which one.

    Every other line of the output is the same across three of the four
    dialects, so without this a `sqlserver` file and a `postgres` file are
    indistinguishable until one of them fails to run.
    """
    model = build(tmp_path, {"DimThing": dimension()})
    assert "-- Dialect: postgres" in generate_sql(model)
    assert "-- Dialect: duckdb" in generate_sql(model, dialect_name="duckdb")


# ---------------------------------------------------------------------------
# Types — what a column says it holds
# ---------------------------------------------------------------------------


# --- uuid --------------------------------------------------------------------


def _uuid_key(tmp_path: pathlib.Path) -> DimensionalModel:
    """Build a dimension whose natural key is a UUID, importing the profile."""
    table = dimension()
    table["attributes"]["d_id"] = {
        "range": "uuid",
        "annotations": {"varda:role": "NATURAL_KEY"},
    }
    return build(tmp_path, {"DimThing": table}, imports=["varda"])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("postgres", "UUID"),
        ("duckdb", "UUID"),
        ("snowflake", "UUID"),
        ("sqlserver", "UNIQUEIDENTIFIER"),
    ],
)
def test_uuid_emits_the_engines_own_type(
    tmp_path: pathlib.Path, name: str, expected: str
) -> None:
    model = _uuid_key(tmp_path)
    assert f'"d_id" {expected}' in generate_sql(model, dialect_name=name)


def test_a_uuid_column_executes(tmp_path: pathlib.Path) -> None:
    """DuckDB has the type, so the round trip is checkable rather than read."""
    model = _uuid_key(tmp_path)
    conn = duckdb.connect()
    conn.execute(generate_sql(model, dialect_name="duckdb"))
    value = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    conn.execute("INSERT INTO mart.dim_thing VALUES (?, 1)", [value])
    kept = conn.execute(
        "SELECT typeof(d_id), CAST(d_id AS VARCHAR) FROM mart.dim_thing"
    ).fetchone()
    assert kept == ("UUID", value)


def test_uuid_is_the_profiles_and_not_the_generators(
    tmp_path: pathlib.Path,
) -> None:
    """A range naming nothing is not a LinkML schema any more.

    Mapping `uuid` by name inside the generator would have worked and would
    have made every model using it unreadable to every other LinkML tool.
    Declaring it in the profile keeps the premise: a Varda model is an
    ordinary LinkML schema.
    """
    model = _uuid_key(tmp_path)
    found = model.view.get_type("uuid")
    assert found is not None
    assert found.typeof == "string"
    assert "V804" not in codes(model)


def test_uuid_without_the_import_is_reported(tmp_path: pathlib.Path) -> None:
    """And reported by `check`, not by a crash three commands later."""
    table = dimension()
    table["attributes"]["d_id"] = {
        "range": "uuid",
        "annotations": {"varda:role": "NATURAL_KEY"},
    }
    model = build(tmp_path, {"DimThing": table})
    found = [f for f in rules.check(model) if f.rule == "V804"]
    assert len(found) == 1
    assert "imports: - varda" in found[0].message


# --- V804 — a range that names nothing ---------------------------------------


def test_v804_a_range_naming_nothing(tmp_path: pathlib.Path) -> None:
    """The failure this closes: clean check, then a GenerationError."""
    table = dimension()
    table["attributes"]["d_id"]["range"] = "intger"
    model = build(tmp_path, {"DimThing": table})
    assert "V804" in codes(model)
    with pytest.raises(GenerationError):
        generate_sql(model)


def test_v804_a_range_naming_a_class(tmp_path: pathlib.Path) -> None:
    table = dimension()
    table["attributes"]["d_id"]["range"] = "DimOther"
    model = build(tmp_path, {"DimThing": table, "DimOther": dimension()})
    found = [f for f in rules.check(model) if f.rule == "V804"]
    assert len(found) == 1
    assert "names a class" in found[0].message


def test_v804_leaves_the_examples_alone() -> None:
    """Every range a shipped model uses resolves."""
    for path in (RETAIL, SNOWFLAKE):
        model = DimensionalModel.load(path, importmap=registry.importmap())
        assert "V804" not in codes(model)


# --- types the schema declares for itself ------------------------------------


def _declared_type(tmp_path: pathlib.Path, **facets: Any) -> DimensionalModel:
    """Build a model whose measure is ranged on a type the schema declares."""
    fact = {
        "annotations": {
            "varda:role": "FACT",
            "varda:fact_type": "TRANSACTION",
            "varda:grain": "d_key",
            "varda:grain_statement": "one row per thing measured once",
        },
        "attributes": {
            "d_key": {
                "range": "integer",
                "annotations": {
                    "varda:role": "FOREIGN_KEY",
                    "varda:references": "DimThing",
                },
            },
            "amount": {
                "range": "money",
                "unit": {"symbol": "EUR"},
                "annotations": {
                    "varda:role": "MEASURE",
                    "varda:additivity": "ADDITIVE",
                    **{f"varda:{k}": v for k, v in facets.items()},
                },
            },
        },
    }
    schema = {
        "id": "https://example.org/t",
        "name": "t",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "varda": "https://w3id.org/varda/",
        },
        "default_prefix": "t",
        "default_range": "string",
        "imports": ["linkml:types"],
        "types": {"money": {"typeof": "decimal"}},
        "classes": {"DimThing": dimension(), "FctThing": fact},
    }
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(schema), encoding="utf-8")
    return DimensionalModel.load(path, importmap=registry.importmap())


def test_a_declared_type_generates(tmp_path: pathlib.Path) -> None:
    """Clean check and then a GenerationError is the worst pair of answers.

    V804 closed the case where a range names nothing. A range naming a type
    the schema declares reached the same place by a different road: LinkML
    resolves it, `varda check --strict` reported nothing, and the generator
    refused. The first answer is the one people trust.
    """
    model = _declared_type(tmp_path, precision=12, scale=2)
    assert not codes(model)
    assert '"amount" NUMERIC(12, 2)' in generate_sql(model)


def test_a_declared_type_carries_facets(tmp_path: pathlib.Path) -> None:
    """Legality is asked of the chain, so `money` takes a precision."""
    model = _declared_type(tmp_path, precision=12, scale=2)
    column = model.facts[0].column("amount")
    assert column is not None
    assert column.type_chain == ("money", "decimal")
    assert (column.precision, column.scale) == (12, 2)


def test_a_declared_type_is_warned_about_like_any_decimal(
    tmp_path: pathlib.Path,
) -> None:
    """The other half of the same failure.

    Asking only the range dropped the facet *and* kept V707 quiet, so a
    measure on a declared decimal type went unwidened and unwarned at once.
    """
    assert "V707" in codes(_declared_type(tmp_path))


def test_the_type_chain_is_walked_nearest_first(
    tmp_path: pathlib.Path,
) -> None:
    """What keeps `uuid` a UUID rather than a 36-character string.

    It is declared `typeof: string`, so a chain resolved the other way
    around would find VARCHAR and hand back a column that sorts and compares
    as text.
    """
    model = _uuid_key(tmp_path)
    column = model.dimensions[0].column("d_id")
    assert column is not None
    assert column.type_chain == ("uuid", "string")
    assert '"d_id" UUID' in generate_sql(model)


def test_a_range_naming_nothing_still_raises(tmp_path: pathlib.Path) -> None:
    """The chain resolves what it can and never invents the rest."""
    table = dimension()
    table["attributes"]["d_id"]["range"] = "intger"
    model = build(tmp_path, {"DimThing": table})
    column = model.dimensions[0].column("d_id")
    assert column is not None
    assert column.type_chain == ("intger",)
    with pytest.raises(GenerationError, match="tried intger"):
        generate_sql(model)


# --- type facets -------------------------------------------------------------


def _sized(**facets: Any) -> dict[str, Any]:
    """Build a dimension carrying one extra column with the given facets."""
    table = dimension()
    rng = facets.pop("range", "string")
    table["attributes"]["sized"] = {
        "range": rng,
        "annotations": {
            "varda:role": "ATTRIBUTE",
            **{f"varda:{k}": v for k, v in facets.items()},
        },
    }
    return table


def _decimal_measure(**facets: Any) -> dict[str, Any]:
    """Build a fact whose one measure carries the given facets."""
    fct = _fact(
        **{
            "varda:grain": ["d_key", "ticket"],
            "varda:grain_statement": "one row per thing per ticket",
        }
    )
    fct["attributes"]["amount"]["annotations"].update(
        {f"varda:{k}": v for k, v in facets.items()}
    )
    return fct


def test_a_length_reaches_the_ddl(tmp_path: pathlib.Path) -> None:
    model = build(tmp_path, {"DimThing": _sized(max_length=80)})
    assert '"sized" VARCHAR(80)' in generate_sql(model)
    assert codes(model) == set()


def test_precision_and_scale_reach_the_ddl(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {
            "FctX": _decimal_measure(precision=18, scale=2),
            "DimThing": dimension(),
        },
    )
    assert '"amount" NUMERIC(18, 2)' in generate_sql(model)
    assert not {"V707", "V803"} & codes(model)


def test_an_undeclared_column_is_unchanged(tmp_path: pathlib.Path) -> None:
    """A model that declares no facet emits what it emitted before.

    The facets are additive or they are a silent migration: every model
    written against an earlier version would otherwise generate different
    DDL on upgrade.
    """
    model = build(tmp_path, {"DimThing": _sized()})
    assert '"sized" VARCHAR,' in generate_sql(model)


def test_scale_survives_a_round_trip(tmp_path: pathlib.Path) -> None:
    """A declared scale keeps digits that a bare NUMERIC drops.

    DuckDB reads a bare `NUMERIC` as `DECIMAL(18, 3)`, so an undeclared unit
    price of 0.123456 is stored as 0.123 and nothing reports it. That is the
    failure the facets exist for, so it is pinned against a real engine
    rather than asserted against the string of the DDL.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    declared = build(
        tmp_path / "a",
        {
            "FctX": _decimal_measure(precision=18, scale=6),
            "DimThing": dimension(),
        },
    )
    bare = build(
        tmp_path / "b", {"FctX": _decimal_measure(), "DimThing": dimension()}
    )

    kept = []
    for model in (declared, bare):
        conn = duckdb.connect()
        conn.execute(generate_sql(model))
        conn.execute("INSERT INTO mart.dim_thing VALUES ('x', 1)")
        conn.execute("INSERT INTO mart.fct_x VALUES (0.123456, 1, 't')")
        kept.append(conn.execute("SELECT amount FROM mart.fct_x").fetchone())

    assert kept[0] == (decimal.Decimal("0.123456"),)
    assert kept[1] == (decimal.Decimal("0.123"),)


def test_v803_a_facet_on_a_range_it_cannot_parameterize(
    tmp_path: pathlib.Path,
) -> None:
    model = build(tmp_path, {"DimThing": _sized(range="date", max_length=10)})
    found = [f for f in rules.check(model) if f.rule == "V803"]
    assert len(found) == 1
    assert "parameterizes nothing" in found[0].message
    assert "string" in found[0].message


def test_v803_a_width_that_is_not_a_number(tmp_path: pathlib.Path) -> None:
    model = build(tmp_path, {"DimThing": _sized(max_length="eighty")})
    assert "V803" in codes(model)


@pytest.mark.parametrize("width", [0, -1])
def test_v803_a_width_of_nothing(tmp_path: pathlib.Path, width: int) -> None:
    """Zero is the interesting one: `VARCHAR(0)` parses and holds nothing."""
    model = build(tmp_path, {"DimThing": _sized(max_length=width)})
    assert "V803" in codes(model)


def test_v803_a_scale_with_no_precision(tmp_path: pathlib.Path) -> None:
    """There is no `NUMERIC(, 2)`, so the half that is there is dropped."""
    model = build(
        tmp_path,
        {"FctX": _decimal_measure(scale=2), "DimThing": dimension()},
    )
    found = [f for f in rules.check(model) if f.rule == "V803"]
    assert len(found) == 1
    assert "no varda:precision" in found[0].message
    assert '"amount" NUMERIC,' in generate_sql(model)


def test_v803_a_scale_larger_than_its_precision(
    tmp_path: pathlib.Path,
) -> None:
    """No database accepts it, so `--force` must not emit it either."""
    model = build(
        tmp_path,
        {
            "FctX": _decimal_measure(precision=4, scale=9),
            "DimThing": dimension(),
        },
    )
    assert "V803" in codes(model)
    sql = generate_sql(model)
    assert '"amount" NUMERIC(4)' in sql
    duckdb.connect().execute(sql)


def test_a_scale_of_zero_is_a_declaration(tmp_path: pathlib.Path) -> None:
    """`NUMERIC(18, 0)` is a whole number stored as a decimal, not a mistake.

    Zero is the one facet value that is legal at its floor, which is why the
    minimum is per facet rather than one shared "positive".
    """
    model = build(
        tmp_path,
        {
            "FctX": _decimal_measure(precision=18, scale=0),
            "DimThing": dimension(),
        },
    )
    assert "V803" not in codes(model)
    assert '"amount" NUMERIC(18, 0)' in generate_sql(model)


def test_v707_a_decimal_measure_saying_nothing(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path, {"FctX": _decimal_measure(), "DimThing": dimension()}
    )
    found = [f for f in rules.check(model) if f.rule == "V707"]
    assert len(found) == 1
    assert found[0].severity == "warning"


def test_v707_leaves_strings_alone(tmp_path: pathlib.Path) -> None:
    """Warning on every undeclared string would fire a dozen times a model.

    A rule that noisy gets exempted, and takes the decimal case with it.
    """
    model = build(tmp_path, {"DimThing": _sized()})
    assert "V707" not in codes(model)


def test_v707_leaves_a_float_measure_alone(tmp_path: pathlib.Path) -> None:
    """A float has no exact precision to declare; that is what a float is."""
    fct = _decimal_measure()
    fct["attributes"]["amount"]["range"] = "float"
    model = build(tmp_path, {"FctX": fct, "DimThing": dimension()})
    assert "V707" not in codes(model)


def test_v126_hierarchy_on_a_fact(tmp_path: pathlib.Path) -> None:
    fct = _fact(
        **{
            "varda:grain": ["d_key"],
            "varda:grain_statement": "one row per thing",
            "varda:hierarchies": [{"name": "nope", "levels": ["d_key", "m"]}],
        }
    )
    model = build(tmp_path, {"FctX": fct, "DimThing": dimension()})
    assert "V606" in codes(model)


def test_a_malformed_hierarchy_does_not_raise(tmp_path: pathlib.Path) -> None:
    """A property access is the wrong place to fail on an unchecked model."""
    table = dimension()
    table["annotations"]["varda:hierarchies"] = ["not a mapping"]
    model = build(tmp_path, {"DimThing": table})
    found = model.table("DimThing")
    assert found is not None
    assert found.hierarchies == ()


def test_generated_docs_render_drill_paths(tmp_path: pathlib.Path) -> None:
    """The path is rendered coarsest-first, the way a reader drills it."""
    model = build(tmp_path, {"DimThing": _geo("country", "region", "city")})
    page = generate_docs(model)
    assert "**Drill path** (geography): `country` → `region` → `city`" in page


def test_generated_docs_say_what_makes_a_level_unique(
    tmp_path: pathlib.Path,
) -> None:
    """A drill path is a list of names, and a name is not an identity.

    `concepts.md` makes this argument and the generated page never showed
    it: `city` holds "Springfield" for cities in three states. The model
    computed the answer on `Level.identity` and nothing read it.
    """
    model = build(tmp_path, {"DimThing": _geo("country", "region", "city")})
    page = generate_docs(model)
    assert (
        "**Unique within the path:** one member is "
        "`country`, `region`, `city` together" in page
    )


def test_the_identity_note_names_a_declared_key() -> None:
    """The whole tuple, not the finest key plus what qualifies it.

    A level may be named by one column and identified by another —
    `product_name` keyed on `gtin` — and the identity then holds `gtin`,
    which the path above it never mentions. Rendering the tuple states the
    answer without putting a stray column in a sentence about the path.
    """
    model = DimensionalModel.load(SNOWFLAKE, importmap=registry.importmap())
    page = generate_docs(model)
    assert "one member is `brand`, `gtin` together" in page


def test_no_identity_note_when_a_level_identifies_itself(
    tmp_path: pathlib.Path,
) -> None:
    """A one-level identity has nothing to qualify, so nothing is said."""
    table = dimension()
    table["attributes"]["only"] = {"annotations": {"varda:role": "ATTRIBUTE"}}
    model = build(tmp_path, {"DimThing": table})
    assert "Unique within the path" not in generate_docs(model)


def test_one_discriminator_not_both(tmp_path: pathlib.Path) -> None:
    """Declaring both a start and a counter must not weaken the constraint.

    `UNIQUE (nk, start, number)` permits two rows sharing a natural key and a
    start that differ only in their counter — strictly weaker than either
    column alone. More metadata must not buy a worse guarantee.
    """
    model = build(
        tmp_path,
        {
            "DimThing": versioned(
                vs=_col("VERSION_START"), vn=_col("VERSION_NUMBER", "integer")
            )
        },
    )
    sql = generate_sql(model)
    assert 'UNIQUE ("d_id", "vs")' in sql
    assert 'UNIQUE ("d_id", "vs", "vn")' not in sql


# ---------------------------------------------------------------------------
# Constraints — how much the database is asked to police
# ---------------------------------------------------------------------------


# --- the levels --------------------------------------------------------------


def _star(tmp_path: pathlib.Path) -> DimensionalModel:
    """Build a dimension and a fact referencing it, with a compound grain."""
    fact = {
        "annotations": {
            "varda:role": "FACT",
            "varda:grain": ["d_key", "line_no"],
            "varda:grain_statement": "one row per thing per line",
        },
        "attributes": {
            "d_key": {
                "range": "integer",
                "required": True,
                "annotations": {
                    "varda:role": "FOREIGN_KEY",
                    "varda:references": "DimThing",
                },
            },
            "line_no": {
                "range": "integer",
                "required": True,
                "annotations": {"varda:role": "DEGENERATE_DIMENSION"},
            },
            "amount": {
                "range": "integer",
                "annotations": {
                    "varda:role": "MEASURE",
                    "varda:additivity": "ADDITIVE",
                },
            },
        },
    }
    return build(tmp_path, {"DimThing": dimension(), "FctThing": fact})


def test_enforced_is_the_default_and_the_header_says_so(
    tmp_path: pathlib.Path,
) -> None:
    """The level is named on every level, not only the unusual ones.

    A header that says which mode produced the file only when the mode is
    surprising is one a reader cannot trust when it is silent.
    """
    sql = generate_sql(_star(tmp_path))
    assert "-- Constraints: enforced" in sql
    assert "PRIMARY KEY" in sql
    assert 'UNIQUE ("d_key", "line_no")' in sql
    assert 'FOREIGN KEY ("d_key")' in sql


def test_asserted_keeps_the_primary_key_and_drops_the_rest(
    tmp_path: pathlib.Path,
) -> None:
    """The cheap claim stays; the two that dominate a load go.

    The primary key is a fraction of the cost, it is the identity of a row
    rather than a claim about it, and every foreign key needs it to exist.
    """
    sql = generate_sql(_star(tmp_path), "mart", "duckdb", "asserted")
    assert '"d_key" INTEGER PRIMARY KEY' in sql
    assert "UNIQUE (" not in sql.split("-- Not enforced")[0]
    assert "FOREIGN KEY" not in sql.split("-- Not enforced")[0]


def test_asserted_records_what_the_loader_is_trusted_to_hold(
    tmp_path: pathlib.Path,
) -> None:
    """A claim that vanishes without a word is one nobody knows is made."""
    sql = generate_sql(_star(tmp_path), "mart", "duckdb", "asserted")
    assert "-- Not enforced here. The loader is trusted to hold:" in sql
    assert '--   UNIQUE ("d_key", "line_no")' in sql
    assert '--   FOREIGN KEY ("d_key")' in sql


def test_asserted_marks_rather_than_drops_where_it_can_be_said(
    tmp_path: pathlib.Path,
) -> None:
    """Snowflake keeps every constraint and marks it trusted.

    `RELY` is the difference between a key the optimizer records and one it
    will eliminate a join against, so dropping the constraint there would
    cost query performance to buy load performance the engine never charged
    for in the first place.
    """
    sql = generate_sql(_star(tmp_path), "mart", "snowflake", "asserted")
    assert '"d_key" INTEGER PRIMARY KEY RELY' in sql
    assert 'UNIQUE ("d_key", "line_no") RELY' in sql
    assert '("d_key") RELY' in sql
    assert "-- Not enforced here" not in sql


def test_none_leaves_bare_tables(tmp_path: pathlib.Path) -> None:
    """Nothing table-level, and no comment either — `none` means bare."""
    sql = generate_sql(_star(tmp_path), "mart", "duckdb", "none")
    for absent in ("PRIMARY KEY", "UNIQUE (", "FOREIGN KEY", "-- Not enforced"):
        assert absent not in sql


def test_a_surrogate_key_keeps_not_null_when_it_loses_its_key(
    tmp_path: pathlib.Path,
) -> None:
    """Half of what a primary key says is free, so that half stays.

    A key column that quietly starts accepting nulls changes what every
    join against it means, and costs nothing to forbid.
    """
    sql = generate_sql(_star(tmp_path), "mart", "duckdb", "none")
    assert '"d_key" INTEGER NOT NULL' in sql


def test_not_null_survives_every_level(tmp_path: pathlib.Path) -> None:
    """`NOT NULL` is what a column is, not a claim about its rows.

    It does not register on a load, and a column that quietly starts
    accepting nulls changes what every query against it means.
    """
    for level in gen_sql.LEVELS:
        sql = generate_sql(_star(tmp_path), "mart", "duckdb", level)
        assert '"line_no" INTEGER NOT NULL' in sql, level


def test_an_unknown_level_raises_naming_the_ones_there_are() -> None:
    """The same treatment an unknown dialect gets."""
    with pytest.raises(GenerationError, match="enforced, asserted, none"):
        gen_sql.enforcement("relaxed", gen_sql.dialect("duckdb"))


def test_every_level_executes_and_the_catalog_shrinks(
    tmp_path: pathlib.Path,
) -> None:
    """The form the database sees, rather than of the DDL."""
    model = _star(tmp_path)
    found = []
    for level in gen_sql.LEVELS:
        con = duckdb.connect()
        con.execute(generate_sql(model, "mart", "duckdb", level))
        row = con.execute(
            "select count(*) from duckdb_constraints() "
            "where constraint_type in ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY')"
        ).fetchone()
        assert row is not None
        found.append(row[0])
    assert found[0] > found[1] > found[2]
    assert found[2] == 0


def test_the_emission_order_does_not_move_with_the_level() -> None:
    """Turning enforcement off must not reshuffle the file.

    The dependency ordering runs whether or not foreign keys are emitted, so
    that a level change produces a diff about constraints and nothing else.
    """
    model = DimensionalModel.load(SNOWFLAKE)
    orders = [
        [
            line
            for line in generate_sql(
                model, "mart", "duckdb", level
            ).splitlines()
            if line.startswith("CREATE TABLE")
        ]
        for level in gen_sql.LEVELS
    ]
    assert orders[0] == orders[1] == orders[2]


def test_a_cycle_only_blocks_while_references_are_emitted(
    tmp_path: pathlib.Path,
) -> None:
    """Two dimensions pointing at each other are legal and unorderable.

    V404 permits the pair, and with the foreign keys inline no CREATE TABLE
    order satisfies them — so `enforced` refuses. Under the weaker levels
    the file declares no references, nothing in it depends on the order, and
    the same model generates.
    """
    left, right = dimension(), dimension()
    left["attributes"]["r_key"] = {
        "range": "integer",
        "annotations": {
            "varda:role": "FOREIGN_KEY",
            "varda:references": "DimRight",
        },
    }
    right["attributes"]["l_key"] = {
        "range": "integer",
        "annotations": {
            "varda:role": "FOREIGN_KEY",
            "varda:references": "DimLeft",
        },
    }
    model = build(tmp_path, {"DimLeft": left, "DimRight": right})
    with pytest.raises(GenerationError, match="DimLeft, DimRight"):
        generate_sql(model)
    for level in ("asserted", "none"):
        sql = generate_sql(model, "mart", "duckdb", level)
        assert sql.count("CREATE TABLE") == 2
        duckdb.connect().execute(sql)


def test_the_level_reaches_the_generator_from_the_flag(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The flag, the context field and the generator, end to end."""
    model_path = tmp_path / "m.yaml"
    _star(tmp_path)
    model_path.write_text(
        (tmp_path / "t.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    code = cli.main(
        [
            "generate",
            str(model_path),
            "--out",
            str(out),
            "--constraints",
            "none",
        ]
    )
    capsys.readouterr()
    assert code == 0
    written = (out / "sql" / "mart.sql").read_text(encoding="utf-8")
    assert "-- Constraints: none" in written
    assert "FOREIGN KEY" not in written


def test_a_generator_written_before_the_field_still_sees_a_level(
    tmp_path: pathlib.Path,
) -> None:
    """`Context` is defaulted, so an extension's generator keeps working."""
    ctx = Context(model=_star(tmp_path), source=tmp_path / "t.yaml")
    assert ctx.constraints == gen_sql.DEFAULT_LEVEL


# --- the assertions the weaker levels move the claims into --------------------


def _seeded(model: DimensionalModel) -> duckdb.DuckDBPyConnection:
    """Load a star with one duplicate key and one orphan reference."""
    con = duckdb.connect()
    con.execute(generate_sql(model, "mart", "duckdb", "none"))
    con.execute(
        'INSERT INTO "mart"."dim_thing" ("d_key", "d_id") '
        "VALUES (1, 'A'), (1, 'A')"
    )
    con.execute(
        'INSERT INTO "mart"."fct_thing" ("d_key", "line_no", "amount") '
        "VALUES (99, 1, 5)"
    )
    return con


def _fired(con: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    """Run every assertion and name the ones that found something."""
    out = []
    for block in sql.split("\n\n"):
        if "SELECT" not in block:
            continue
        label = block.splitlines()[0]
        query = block[block.index("SELECT") :].rstrip().rstrip(";")
        rows = con.execute(query).fetchall()
        if rows and any(row[-1] for row in rows):
            out.append(label)
    return out


def test_the_assertions_find_what_the_constraints_would_have(
    tmp_path: pathlib.Path,
) -> None:
    """The duplicate and the orphan, against a database that enforced neither.

    This is the whole claim of the file: the same failures, caught once per
    load instead of once per row.
    """
    model = _star(tmp_path)
    fired = _fired(_seeded(model), generate_assertions(model))
    assert fired == [
        '-- dim_thing: PRIMARY KEY ("d_key")',
        '-- dim_thing: UNIQUE ("d_id")',
        "-- fct_thing.d_key -> dim_thing.d_key",
    ]


def test_the_assertions_are_quiet_on_data_that_holds(
    tmp_path: pathlib.Path,
) -> None:
    """No false positives, which is what decides whether they get run."""
    model = _star(tmp_path)
    con = duckdb.connect()
    con.execute(generate_sql(model, "mart", "duckdb", "none"))
    con.execute(
        'INSERT INTO "mart"."dim_thing" ("d_key", "d_id") '
        "VALUES (1, 'A'), (2, 'B')"
    )
    con.execute(
        'INSERT INTO "mart"."fct_thing" ("d_key", "line_no", "amount") '
        "VALUES (1, 1, 5)"
    )
    assert _fired(con, generate_assertions(model)) == []


def test_the_assertions_read_nulls_the_way_the_constraint_does(
    tmp_path: pathlib.Path,
) -> None:
    """SQL admits both, so an assertion standing in for it must too.

    A `UNIQUE` key holding a null is not a duplicate and a null foreign key
    is not an orphan. An assertion stricter than the constraint it replaces
    reports rows the database would have taken, and a check that cries wolf
    is one people switch off.
    """
    # Purpose-built: the star above requires its keys, and a required
    # column cannot show what happens to a null one.
    nullable = dimension()
    nullable["attributes"]["d_id"] = {
        "annotations": {"varda:role": "NATURAL_KEY"}
    }
    fact = {
        "annotations": {
            "varda:role": "FACT",
            "varda:grain": ["d_key", "line_no"],
            "varda:grain_statement": "one row per thing per line",
        },
        "attributes": {
            "d_key": {
                "range": "integer",
                "annotations": {
                    "varda:role": "FOREIGN_KEY",
                    "varda:references": "DimThing",
                },
            },
            "line_no": {
                "range": "integer",
                "annotations": {"varda:role": "DEGENERATE_DIMENSION"},
            },
        },
    }
    model = build(tmp_path, {"DimThing": nullable, "FctThing": fact})
    con = duckdb.connect()
    con.execute(generate_sql(model, "mart", "duckdb", "none"))
    con.execute(
        'INSERT INTO "mart"."dim_thing" ("d_key", "d_id") '
        "VALUES (1, NULL), (2, NULL)"
    )
    con.execute(
        'INSERT INTO "mart"."fct_thing" ("d_key", "line_no") VALUES (NULL, 1)'
    )
    assert _fired(con, generate_assertions(model)) == []


def test_the_assertions_state_the_ddl_claims_and_no_others() -> None:
    """One claim, computed once.

    The DDL renders each unique claim as a constraint and the assertions
    render each as a query; both read `Table.unique_claims`, so the two
    files cannot come to disagree about what the model said.
    """
    model = DimensionalModel.load(RETAIL)
    in_ddl = generate_sql(model).count("UNIQUE (")
    in_assertions = generate_assertions(model).count(": UNIQUE (")
    assert in_ddl == in_assertions > 0


def test_the_assertions_parse_everywhere_including_the_absent_dialects() -> (
    None
):
    """Plain SQL is the point: a lakehouse gets the check it cannot enforce.

    `bigquery` and `oracle` have no Varda dialect and cannot be sent DDL at
    all, and this file still runs against both. That is the gap it closes.
    """
    blocks = [
        block[block.index("SELECT") :]
        for block in generate_assertions(DimensionalModel.load(RETAIL)).split(
            "\n\n"
        )
        if "SELECT" in block
    ]
    assert blocks
    for read in (
        "postgres",
        "duckdb",
        "snowflake",
        "tsql",
        "bigquery",
        "oracle",
        "databricks",
    ):
        for block in blocks:
            sqlglot.parse_one(block, read=read)


def test_a_self_reference_joins_two_distinct_names(
    tmp_path: pathlib.Path,
) -> None:
    """A reporting line joins a table to itself, so one alias will not do."""
    employee = dimension()
    employee["attributes"]["manager_key"] = {
        "range": "integer",
        "annotations": {
            "varda:role": "FOREIGN_KEY",
            "varda:references": "DimEmployee",
        },
    }
    sql = generate_assertions(build(tmp_path, {"DimEmployee": employee}))
    assert 'FROM "mart"."dim_employee" AS "src"' in sql
    assert 'LEFT JOIN "mart"."dim_employee" AS "tgt"' in sql
    duckdb.connect().execute(
        'CREATE SCHEMA mart; CREATE TABLE "mart"."dim_employee" '
        '("d_key" INTEGER, "d_id" VARCHAR, "manager_key" INTEGER);'
        + sql[sql.index("SELECT") :]
    )


def test_the_assertions_say_so_when_a_model_claims_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """An empty file reads as a generator that failed quietly."""
    bare = {
        "annotations": {"varda:role": "DIMENSION", "varda:scd": "TYPE_1"},
        "attributes": {"note": {"annotations": {"varda:role": "ATTRIBUTE"}}},
    }
    sql = generate_assertions(build(tmp_path, {"DimBare": bare}))
    assert "no claim that a database enforces" in sql


# ---------------------------------------------------------------------------
# Interop — the claim that a Varda model is an ordinary LinkML schema
# ---------------------------------------------------------------------------

# The premise of the package, stated in `SPEC.md` §1 and in the profile's own
# description: a model carrying Varda annotations is a legal LinkML schema
# that every other LinkML tool reads, ignoring what it does not understand.
#
# It was argued for three releases and never run. It was false. `imports:
# - varda` reached a profile declaring four annotation classes and five
# enums, and a LinkML import is a union — so `gen-erdiagram` drew
# `TableAnnotations` as a table, `gen-pydantic` emitted a model for `Level`,
# and `gen-owl` wrote an `owl:Class` for each. These tests run the real
# generators, because reading the claim is what let it go wrong.

#: Every name the profile declares. None may appear in output generated from
#: a domain model: they are vocabulary a validator reads, not data a model
#: holds.
VOCABULARY = (
    "TableAnnotations",
    "ColumnAnnotations",
    "Hierarchy",
    "Level",
    "TableRole",
    "ColumnRole",
    "Additivity",
    "SlowlyChangingType",
    "FactType",
)


def _generated(generator: Any, model: pathlib.Path) -> str:
    """Run one stock LinkML generator over a model, through the import map.

    Warnings are captured rather than filtered. `OwlSchemaGenerator`
    announces two defaults that will change, through a helper that calls
    `warnings.filterwarnings("default", ...)` itself — which re-arms the
    filter as it emits and defeats a `filterwarnings` mark. What is asserted
    below is which names appear, and no OWL axiom default can move that.
    """
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("ignore")
        instance = generator(str(model), importmap=registry.importmap())
        return str(instance.serialize())


@pytest.mark.parametrize("model", [RETAIL, SNOWFLAKE])
@pytest.mark.parametrize(
    "generator",
    [
        pytest.param("erdiagramgen.ERDiagramGenerator", id="gen-erdiagram"),
        pytest.param("jsonschemagen.JsonSchemaGenerator", id="gen-json-schema"),
        pytest.param("pydanticgen.PydanticGenerator", id="gen-pydantic"),
        pytest.param("owlgen.OwlSchemaGenerator", id="gen-owl"),
    ],
)
def test_no_stock_generator_emits_varda_vocabulary(
    generator: str, model: pathlib.Path
) -> None:
    """The claim, executed against the four generators the README names.

    Both examples, because they differ in the way that matters: `retail.yaml`
    imports the profile for `uuid` and `snowflake.yaml` does not, and the
    polluted one was the one following the documentation.
    """
    module, _, name = generator.partition(".")
    imported = importlib.import_module(f"linkml.generators.{module}")
    out = _generated(getattr(imported, name), model)
    found = [word for word in VOCABULARY if word in out]
    assert not found, f"{generator} emitted {found} from {model.name}"


def test_a_generator_sees_exactly_the_tables_the_model_declares() -> None:
    """Counted rather than pattern-matched, so a rename cannot hide a leak.

    `VOCABULARY` above is a list of names and goes stale the moment the
    profile grows one. This says the stronger thing: what a stock tool sees
    is the model's own classes and nothing whatsoever besides.
    """
    model = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    assert set(model.view.all_classes()) == {t.name for t in model.tables}


def test_the_types_schema_declares_only_types() -> None:
    """The regression guard, on the file that ships.

    A class or an enum here is not vocabulary a model gains. It is output a
    model did not ask for, in every generator that walks the class list.
    """
    view = SchemaView(str(TYPES))
    assert not view.schema.classes
    assert not view.schema.enums
    assert sorted(view.schema.types) == ["uuid"]


def test_the_profile_is_not_importable_and_the_types_schema_is() -> None:
    """The map is what a model imports, so it carries the importable one."""
    varda = registry.varda_extension()
    assert varda.profile == PROFILE
    assert varda.types == TYPES
    assert registry.importmap()["varda"] == str(TYPES.with_suffix(""))


def test_a_types_schema_declaring_a_class_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """Caught at load, where the message can name the file and the class."""
    schema = tmp_path / "acme_types.yaml"
    schema.write_text(
        yaml.safe_dump(
            {
                "id": "https://example.org/acme/types",
                "name": "acme_types",
                "prefixes": {"acme": "https://example.org/acme/"},
                "default_prefix": "acme",
                "default_range": "string",
                "classes": {"Leaks": {"attributes": {"x": {}}}},
            }
        ),
        encoding="utf-8",
    )
    ext = Extension(name="acme", prefix="acme", types=schema)
    with (
        pytest.raises(ExtensionError, match="may declare types only"),
        registry.using(ext),
    ):
        pass


def test_an_extension_declaring_no_types_is_not_in_the_map(
    tmp_path: pathlib.Path,
) -> None:
    """An extension that only adds annotations has nothing a model imports.

    Being in the map is what let Varda's own profile pollute every model
    that followed the documentation, so the map now carries only what is
    meant to be imported.
    """
    profile = tmp_path / "acme.yaml"
    profile.write_text(
        yaml.safe_dump(
            {
                "id": "https://example.org/acme",
                "name": "acme",
                "prefixes": {"acme": "https://example.org/acme/"},
                "default_prefix": "acme",
                "default_range": "string",
                "classes": {
                    "AcmeTableAnnotations": {
                        "annotations": {"acme:applies_to": "table"},
                        "attributes": {"owner": {"range": "string"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ext = Extension(name="acme", prefix="acme", profile=profile)
    with registry.using(ext):
        assert registry.declared_annotations("table") >= {"acme:owner"}
        assert "acme" not in registry.importmap()


def test_the_import_map_is_json_a_generator_can_read(
    capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    """`--json` exists so the map can be handed to the tools it is for.

    LinkML's generators all take `--importmap FILE` and read JSON. Printing
    `prefix=path` is legible and is not that, so a user following the docs
    had to reformat it by hand.
    """
    assert cli.main(["importmap", "--json"]) == 0
    written = tmp_path / "im.json"
    written.write_text(capsys.readouterr().out, encoding="utf-8")
    loaded = json.loads(written.read_text(encoding="utf-8"))
    assert loaded == registry.importmap()
    imported = importlib.import_module("linkml.generators.erdiagramgen")
    out = imported.ERDiagramGenerator(str(RETAIL), importmap=loaded).serialize()
    assert "DimCustomer" in out


def test_uuid_still_resolves_through_the_import() -> None:
    """The one thing the import is for still works.

    Splitting the profile would be no fix at all if it took `range: uuid`
    with it: the type is why the import exists.
    """
    model = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    assert "uuid" in model.view.all_types()
    assert "UUID" in generate_sql(model)


# ---------------------------------------------------------------------------
# SQLAlchemy — the same model as objects, checked against the DDL
# ---------------------------------------------------------------------------

# The generator is safe to own because it does not have to be trusted. Its
# module and `sql/mart.sql` are two renderings of one model, and a real
# database is asked whether they agree — so a change to either that the other
# does not follow is a failing test rather than a discovery six months later.


def _module(source: str, tmp_path: pathlib.Path, name: str) -> Any:
    """Import a generated module, the way its reader would."""
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sa_dialect(module: Any) -> Any:
    """Build one SQLAlchemy dialect, without tripping `no-untyped-call`.

    `sqlalchemy.dialects.postgresql.dialect` is assigned at import time
    rather than declared, so it reaches mypy untyped. Reading it through an
    `Any` says that in one place instead of putting a `type: ignore` at every
    call site.
    """
    factory: Any = module.dialect
    return factory()


def _duckdb_catalog(con: duckdb.DuckDBPyConnection) -> tuple[Any, Any]:
    """Read back what a database actually built, columns and constraints."""
    columns = con.execute(
        "select table_name, column_name, data_type, is_nullable "
        "from information_schema.columns where table_schema = 'mart' "
        "order by table_name, column_name"
    ).fetchall()
    constraints = con.execute(
        "select table_name, constraint_type, "
        "constraint_column_names::varchar from duckdb_constraints() "
        "where schema_name = 'mart' "
        "order by table_name, constraint_type, 3"
    ).fetchall()
    return columns, constraints


@pytest.mark.parametrize("model", [RETAIL, SNOWFLAKE])
@pytest.mark.parametrize("level", gen_sql.LEVELS)
def test_the_module_and_the_ddl_build_the_same_database(
    model: pathlib.Path, level: str, tmp_path: pathlib.Path
) -> None:
    """The check the whole generator rests on.

    Both renderings are executed and the resulting catalogs compared, rather
    than the two texts diffed: SQLAlchemy quotes only what needs quoting and
    spells `TIMESTAMP WITHOUT TIME ZONE` in full, so the files never match
    character for character even when they say the same thing.

    Every level, because the levels are exactly where the two could disagree
    about what to emit — and one of them did. Under `none` a surrogate key
    keeps `NOT NULL`, and a model whose surrogate keys are not declared
    `required` was what told the two apart.
    """
    loaded = DimensionalModel.load(model, importmap=registry.importmap())
    module = _module(
        gen_sqlalchemy.generate(loaded, "mart", level),
        tmp_path,
        f"mart_{level}",
    )
    theirs = duckdb.connect()
    theirs.execute(generate_sql(loaded, "mart", "duckdb", level))
    ours = duckdb.connect()
    ours.execute("CREATE SCHEMA mart;")
    for table in module.metadata.sorted_tables:
        ours.execute(
            str(CreateTable(table).compile(dialect=_sa_dialect(sa_postgresql)))
        )
    assert _duckdb_catalog(theirs) == _duckdb_catalog(ours)


def test_the_type_tables_cover_the_same_ranges() -> None:
    """Two lists of the ranges Varda knows are two answers to one question.

    The SQLAlchemy names are a rendering of the decision `gen_sql.TYPES`
    makes, not a second decision, so a range added to one and not the other
    is a generator that raises on a model the other one builds.
    """
    assert set(gen_sqlalchemy.TYPES) == set(gen_sql.TYPES)


#: Where a generic SQLAlchemy type does not render what `gen_sql` names for
#: an engine. Pinned rather than corrected: what a type means on each engine
#: is SQLAlchemy's to know, the same way what needs quoting is, and a module
#: that names a dialect is not the database-neutral artifact this is for.
#: Listed so the divergence is declared and a change in either side is caught.
KNOWN_TYPE_DIVERGENCE = {
    # `sa.DateTime()` is `DATETIME` at every SQL Server version. The DDL
    # emits `DATETIME2` under `--dialect sqlserver`, which is where an
    # engine-specific decision belongs.
    ("sqlserver", "datetime"): "DATETIME",
    # `Date` and `Time` are gated on the *connected* server's version and
    # resolve correctly against any SQL Server 2008 or later. This is what
    # they compile to with nothing connected, which is the only way a test
    # can ask.
    ("sqlserver", "date"): "DATETIME",
    ("sqlserver", "time"): "DATETIME",
    # T-SQL treats `DOUBLE PRECISION` as a synonym for `FLOAT(53)`, so this
    # is two spellings of one type and nothing turns on it.
    ("sqlserver", "float"): "DOUBLE PRECISION",
    ("sqlserver", "double"): "DOUBLE PRECISION",
    # An unsized string. The DDL refuses this under `--dialect sqlserver`
    # rather than emitting it, because a `VARCHAR(max)` cannot be a key
    # column; SQLAlchemy widens instead.
    ("sqlserver", "string"): "VARCHAR(max)",
    ("sqlserver", "uri"): "VARCHAR(max)",
    ("sqlserver", "uriorcurie"): "VARCHAR(max)",
    ("sqlserver", "ncname"): "VARCHAR(max)",
}


@pytest.mark.parametrize("dialect_name", ["postgres", "sqlserver"])
def test_every_range_renders_what_it_is_expected_to(dialect_name: str) -> None:
    """The generic types against the dialect tables, agreement and all.

    They agree everywhere on PostgreSQL, which is the base both are written
    against. On SQL Server they differ in nine places, every one of them
    listed above — so this is not a test that they match but a test that the
    divergence is the one that was decided on. A new disagreement, from
    either side, fails here.

    `WITHOUT TIME ZONE` is PostgreSQL spelling out the default the DDL leaves
    implicit, and is not a divergence.
    """
    sql = gen_sql.dialect(dialect_name)
    compiler = {"postgres": sa_postgresql, "sqlserver": sa_mssql}[dialect_name]
    for rng in gen_sql.TYPES:
        built = eval(  # noqa: S307 - the table's own values, no input
            gen_sqlalchemy.TYPES[rng] + "()",
            {"sa": sa},
        )
        rendered = built.compile(dialect=_sa_dialect(compiler))
        rendered = rendered.replace(" WITHOUT TIME ZONE", "")
        expected = KNOWN_TYPE_DIVERGENCE.get(
            (dialect_name, rng), sql.type_of(rng)
        )
        assert rendered == expected, f"{dialect_name}/{rng}"


def test_the_module_names_no_database() -> None:
    """The artifact is worth having because it does not pick an engine.

    Identifier quoting was already SQLAlchemy's to decide, on the grounds
    that a per-dialect list is maintained by people who do it for a living.
    Types are the same kind of knowledge kept by the same people, so pinning
    one engine's spelling into every module would be taking half of that
    judgment back. Where Varda does have an engine-specific opinion, it is in
    the DDL, and the module header says where.
    """
    loaded = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    source = gen_sqlalchemy.generate(loaded)
    for engine in ("mssql", "postgresql", "duckdb", "snowflake", "variant"):
        assert engine not in source.replace("sqlserver", ""), engine
    assert "sql/mart.sql" in source


def test_a_weaker_level_records_the_claims_it_drops(
    tmp_path: pathlib.Path,
) -> None:
    """A claim that vanishes without a word is one nobody knows is made.

    The DDL writes them into a comment block. Here they are data, which is
    the one place this module has to put them — and the more useful of the
    two, since a consumer can read it.
    """
    loaded = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    module = _module(
        gen_sqlalchemy.generate(loaded, "mart", "asserted"),
        tmp_path,
        "asserted_claims",
    )
    bridge = module.metadata.tables["mart.bridge_customer_segment"]
    assert not bridge.constraints - {bridge.primary_key}
    assert bridge.info["unique_unenforced"] == [["customer_key", "segment_key"]]


def test_a_surrogate_key_is_not_generated_by_the_database(
    tmp_path: pathlib.Path,
) -> None:
    """`autoincrement=False`, or SQLAlchemy emits SERIAL.

    It reads an integer primary key as one the database generates. A
    warehouse surrogate key is assigned by the loader, and a sequence default
    would quietly fill in for a load that left it null — which is the bug the
    key exists to make impossible. The first prototype died on DuckDB with
    `Type with name SERIAL does not exist`.
    """
    loaded = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    source = gen_sqlalchemy.generate(loaded)
    assert "autoincrement=False" in source
    module = _module(source, tmp_path, "no_serial")
    rendered = str(
        CreateTable(module.metadata.tables["mart.dim_product"]).compile(
            dialect=_sa_dialect(sa_postgresql)
        )
    )
    assert "SERIAL" not in rendered
    assert "product_key INTEGER NOT NULL" in rendered


def test_the_annotations_reach_runtime(tmp_path: pathlib.Path) -> None:
    """The reason this is worth more than a second rendering of the DDL.

    `info` is a mapping SQLAlchemy stores and never interprets, so a consumer
    holding the module reads what the model claimed with no Varda installed.
    Until this generator, `varda:additivity` and `varda:semi_additive_over`
    reached no machine-readable output at all.
    """
    loaded = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    module = _module(gen_sqlalchemy.generate(loaded), tmp_path, "runtime")
    fact = module.metadata.tables["mart.fct_inventory"]
    assert fact.info["role"] == "FACT"
    assert fact.info["grain_statement"].startswith("one row per")
    measure = fact.c["quantity_on_hand"]
    assert measure.info["additivity"] == "SEMI_ADDITIVE"
    assert measure.info["semi_additive_over"] == "date_key"

    date = module.metadata.tables["mart.dim_date"]
    assert [h["name"] for h in date.info["hierarchies"]] == [
        "calendar",
        "fiscal",
    ]


def test_a_description_becomes_a_database_comment(
    tmp_path: pathlib.Path,
) -> None:
    """Where `information_schema` and every BI tool look.

    The DDL writes a description as a `--` comment and the parser throws it
    away. Through this path it is a `COMMENT ON`, and it persists.
    """
    loaded = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    module = _module(gen_sqlalchemy.generate(loaded), tmp_path, "comments")
    emitted: list[str] = []

    def record(statement: Any, *_: Any, **__: Any) -> None:
        emitted.append(str(statement.compile(dialect=engine.dialect)))

    engine = sa.create_mock_engine("postgresql://", record)
    module.metadata.create_all(engine)
    comments = [s for s in emitted if s.strip().startswith("COMMENT ON")]
    assert any("COMMENT ON TABLE mart.dim_customer" in c for c in comments)
    assert any("COMMENT ON COLUMN mart.dim_customer" in c for c in comments)


def test_the_emitted_module_is_already_formatted(
    tmp_path: pathlib.Path,
) -> None:
    """Generated Python is read by people and lands in somebody's repository.

    A module that reformats on its reader's first commit produces a diff
    nobody asked for, so the generator emits what `ruff format` would. The
    drill paths of the date dimension are what force the wrapping: written
    flat they run to 192 columns.
    """
    loaded = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    source = gen_sqlalchemy.generate(loaded)
    assert max(len(line) for line in source.splitlines()) <= 80
    written = tmp_path / "mart.py"
    written.write_text(source, encoding="utf-8")
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff is not on PATH")
    # S603: the arguments are a resolved executable, literals, and a path
    # written two lines above into a directory pytest made.
    run = subprocess.run(  # noqa: S603
        [ruff, "format", "--check", "--line-length", "80", str(written)],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_module_imports_nothing_of_vardas() -> None:
    """The consumer installs SQLAlchemy, not Varda.

    A generated artifact that needs its generator at runtime is a dependency
    nobody agreed to, and it would put Varda in the import graph of every
    service that reads the warehouse.
    """
    loaded = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    source = gen_sqlalchemy.generate(loaded)
    assert "varda" not in source.replace("Generated by varda", "")
    imports = [
        ln for ln in source.splitlines() if ln.startswith(("import ", "from "))
    ]
    assert imports == ["import sqlalchemy as sa"]


def test_an_unsized_string_is_refused_by_the_ddl_and_not_by_the_module(
    tmp_path: pathlib.Path,
) -> None:
    """Where the two generators are allowed to disagree, and why.

    `VARCHAR(max)` on SQL Server parses and then cannot carry the `UNIQUE` a
    natural key needs, so the DDL refuses it under that dialect. The module
    names no dialect and so has nothing to refuse on — and nothing is lost,
    because the run that would have written a bad `CREATE TABLE` is the run
    the DDL stops, before anything is written.
    """
    model = build(tmp_path, {"DimThing": dimension()})
    with pytest.raises(GenerationError, match="varda:max_length"):
        generate_sql(model, "mart", "sqlserver")
    # And the module does not refuse it, because it names no engine: an
    # unsized string is `VARCHAR` on the engines that have one and
    # SQLAlchemy's business on the ones that do not.
    assert "sa.String()" in gen_sqlalchemy.generate(model)


def test_generation_is_deterministic() -> None:
    """Same model in, same bytes out — the property the whole tree rests on."""
    loaded = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    assert gen_sqlalchemy.generate(loaded) == gen_sqlalchemy.generate(loaded)


# ---------------------------------------------------------------------------
# Portability — what only breaks on somebody else's machine
# ---------------------------------------------------------------------------

#: Calls whose default is the *locale's* encoding rather than UTF-8, and
#: which therefore do one thing here and another on Windows.
TEXT_CALLS = frozenset({"open", "read_text", "write_text"})


def _unencoded(path: pathlib.Path) -> list[int]:
    """Give the lines in one file that read or write text without saying how.

    A binary `open` is not a finding: it names no encoding because it
    decodes nothing. The mode is only consulted for `open`, since the first
    argument to `write_text` is the payload and a string holding a `b` is
    not a mode.
    """
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or any(
            k.arg == "encoding" for k in node.keywords
        ):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", "")
        mode = node.args[0] if node.args else None
        binary = (
            name == "open"
            and isinstance(mode, ast.Constant)
            and "b" in str(mode.value)
        )
        decodes = (name in TEXT_CALLS and not binary) or (
            name == "run"
            and any(
                k.arg in {"text", "universal_newlines"} for k in node.keywords
            )
        )
        if decodes:
            found.append(node.lineno)
    return found


def test_every_text_call_names_its_encoding() -> None:
    """`write_text` with no encoding writes cp1252 on Windows.

    `Path.write_text` and `subprocess.run(text=True)` both default to the
    locale's encoding, which is UTF-8 on the machines this was written on
    and cp1252 on `windows-latest`. The emitted SQLAlchemy module's header
    carries an em dash; written through the default it became byte 0x97,
    and both the import machinery and `ruff format` read a `.py` file as
    UTF-8. Eleven tests failed on Windows and nowhere else.

    Walked rather than listed, for the reason
    `test_every_cached_lookup_is_reset` is: the next such call gets written
    by somebody who has never seen this failure, and a Windows runner is a
    slow way to find out.
    """
    faults = {
        str(path.relative_to(ROOT)): lines
        for folder in ("src/varda", "tests", "scripts")
        for path in sorted((ROOT / folder).glob("*.py"))
        if (lines := _unencoded(path))
    }
    assert not faults


# ---------------------------------------------------------------------------
# The public boundary — what an extension is entitled to import
# ---------------------------------------------------------------------------


def test_an_extension_needs_nothing_but_the_interface_module(
    tmp_path: pathlib.Path,
) -> None:
    """A rule can be written against `varda.ext` alone.

    Four places said so — this module's own fixture among them — while
    `Finding` and `RuleSet` lived in `varda.rules`, so the claim was false
    in exactly the arrangement it was making a promise about. Written here
    with the imports the documentation shows, so the page and the package
    fail together or not at all.
    """
    rs = ext_module.RuleSet(tag="BND")
    rs.rule("BND001", "error", "Everything a rule needs is in varda.ext")(
        lambda m: iter(
            [
                ext_module.Finding("BND001", "error", str(t), "reported")
                for t in m.tables
            ]
        )
    )
    model = build(tmp_path, {"DimThing": dimension()})
    with registry.using(_bare("bnd", "bnd", rule_tag="BND", rules=rs)):
        assert "BND001" in {f.rule for f in rules.check(model)}


def test_the_older_import_path_still_reaches_the_same_class() -> None:
    """`varda.rules` re-exports what moved, so a shipped extension survives.

    An extension pinned against 0.3 imports these from `varda.rules`. The
    move is a correction to where the boundary is drawn, not a reason to
    break somebody who followed the documentation as it stood.
    """
    assert rules.Finding is ext_module.Finding
    assert rules.RuleSet is ext_module.RuleSet
    assert varda.Finding is ext_module.Finding


def _runtime_varda_imports(path: pathlib.Path) -> set[str]:
    """Name every `varda` module a file imports outside `TYPE_CHECKING`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "TYPE_CHECKING" in ast.unparse(node.test)
        for sub in ast.walk(node)
    }
    return {
        str(node.module)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and id(node) not in guarded
        and (node.module or "").startswith("varda")
    }


def test_the_worked_example_imports_only_the_interface_module() -> None:
    """The fixture is held to the sentence it is there to demonstrate.

    Read rather than asserted in prose, because prose is what failed: the
    fixture's docstring claimed this while the line twelve below it
    contradicted the claim, and nothing anywhere could tell. This is the
    narrow form of the conformance scan `SPEC.md` puts on the roadmap, run
    against the one extension this repository ships.

    Type-only imports are excluded deliberately. A rule annotated with the
    type of its argument names `DimensionalModel`, which `varda.ext` names
    too; whether that read API is public is a separate and open question,
    and this test would otherwise assert an answer to it.
    """
    fixture = ROOT / "tests" / "fixtures" / "acme_ext" / "__init__.py"
    assert _runtime_varda_imports(fixture) == {"varda.ext"}


# ---------------------------------------------------------------------------
# Rules that raise — somebody else's code failing inside this one
# ---------------------------------------------------------------------------


def _exploding(tag: str = "BOOM") -> RuleSet:
    """Build a rule set whose one rule raises when it is run."""

    def boom(_model: DimensionalModel) -> Iterator[rules.Finding]:
        msg = "an index this rule assumed"
        raise IndexError(msg)
        yield  # pragma: no cover — unreachable, and what makes this a rule

    rs = RuleSet(tag=tag)
    rs.rule(f"{tag}101", "error", "A rule with a bug in it")(boom)
    return rs


def test_a_rule_that_raises_names_itself_and_the_extension_that_shipped_it(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Flag a failing rule the way a failing generator is already flagged.

    `_collect` has wrapped generators since 0.1 on the argument that the
    CLI's job is to name the culprit rather than hand over a traceback
    through somebody else's code. The checking path had no such guard, and
    it is the path that runs on every pull request.
    """
    model_path = tmp_path / "t.yaml"
    build(tmp_path, {"DimThing": dimension()})
    with registry.using(
        _bare("boom", "boom", rule_tag="BOOM", rules=_exploding())
    ):
        assert cli.main(["check", str(model_path)]) == 1
    err = capsys.readouterr().err
    assert "BOOM101" in err
    assert "boom" in err
    assert "IndexError" in err
    assert "Traceback" not in err


def test_a_rule_that_raises_stops_a_run_rather_than_reporting_less() -> None:
    """The other option was to skip it, and it is worse.

    A rule that cannot run is a check that is not happening. Reporting the
    rules that survived and exiting zero is the tool saying a model
    conforms when part of the question was never asked — which is the
    failure `varda check` exists to prevent.
    """
    model = DimensionalModel.load(RETAIL, importmap=registry.importmap())
    with (
        registry.using(
            _bare("boom", "boom", rule_tag="BOOM", rules=_exploding())
        ),
        pytest.raises(RuleError, match="BOOM101"),
    ):
        rules.check(model)


def test_a_rule_that_raises_leaves_no_artifacts_behind(
    tmp_path: pathlib.Path,
) -> None:
    """Generation validates first, so the guard reaches it too.

    The same property `test_generate_fails_closed` asserts for a generator
    that raises, for the layer above it: nothing is written, because
    nothing got as far as being generated.
    """
    out = tmp_path / "out"
    with registry.using(
        _bare("boom", "boom", rule_tag="BOOM", rules=_exploding())
    ):
        code = cli.main(["generate", str(RETAIL), "--out", str(out)])
    assert code == 1
    assert not out.exists()


#: A model that breaks as much as one model can, so the walk below meets as
#: many `Finding(...)` sites as possible. Not an example of anything.
_EVERYTHING_WRONG: dict[str, Any] = {
    "FctNoGrain": {
        "annotations": {"varda:role": "FACT"},
        "attributes": {
            "d_key": {
                "range": "integer",
                "annotations": {
                    "varda:role": "FOREIGN_KEY",
                    "varda:references": "Nowhere",
                },
            },
            "amount": {
                "range": "decimal",
                "annotations": {"varda:role": "MEASURE"},
            },
            "orphan": {},
        },
    },
    "DimBad": {
        "annotations": {
            "varda:role": "DIMENSION",
            "varda:scd": "TYPE_2",
            "varda:hierarchies": [
                {"name": "h", "levels": ["missing_level"]},
            ],
        },
        "attributes": {
            "d_key": {
                "range": "integer",
                "annotations": {"varda:role": "SURROGATE_KEY"},
            },
            "valid_to": {
                "range": "datetime",
                "annotations": {"varda:role": "VERSION_END"},
            },
        },
        "unique_keys": {"nothing": {"unique_key_slots": ["not_a_column"]}},
    },
    "Dim_Bad": {
        "annotations": {"varda:role": "DIMENSION"},
        "attributes": {
            "d_key": {
                "range": "integer",
                "annotations": {"varda:role": "SURROGATE_KEY"},
            },
            "nk": {"required": True, "annotations": {"varda:role": "NAT_KEY"}},
        },
    },
}


def test_every_finding_names_a_table_the_model_has(
    tmp_path: pathlib.Path,
) -> None:
    """The convention that lets one place answer "which file is this in".

    Every subject a rule states begins with a table name — a table is its
    own name, and a column, a hierarchy and a unique key are all rendered
    `Table.thing`. `rules._subject_table` reads the origin of a finding out
    of that, so a rule inventing a subject of another shape would silently
    stop being attributed to any file. Provoked against a model that fails
    widely, so the sixty-odd `Finding(...)` sites are covered by what they
    emit rather than by being listed here.
    """
    model = build(tmp_path, _EVERYTHING_WRONG)
    found = rules.check(model)
    names = {t.name for t in model.tables}
    unattributed = sorted(
        f"{f.rule}: {f.subject}"
        for f in found
        if rules._subject_table(f.subject) not in names
    )
    assert not unattributed
    # A lower bound, so the model above cannot quietly stop provoking
    # anything and leave the walk with nothing to walk.
    assert len({f.rule for f in found}) >= 15


# ---------------------------------------------------------------------------
# Imports — when a model is more than one file
# ---------------------------------------------------------------------------


def _schema(name: str, classes: dict[str, Any], imports: list[str]) -> str:
    return str(
        yaml.safe_dump(
            {
                "id": f"https://example.org/{name}",
                "name": name,
                "prefixes": {
                    "linkml": "https://w3id.org/linkml/",
                    "varda": "https://w3id.org/varda/",
                },
                "default_prefix": name,
                "default_range": "string",
                "imports": ["linkml:types", *imports],
                "classes": classes,
            }
        )
    )


def two_files(
    tmp_path: pathlib.Path,
    local: dict[str, Any],
    shared: dict[str, Any],
) -> pathlib.Path:
    """Write a mart that imports a second schema, and return the mart's path.

    Two files because that is the whole subject. A conformed dimension is
    declared once and imported by every mart that uses it, which is what
    `imports:` is for and what a dimensional tool should expect to meet.
    """
    (tmp_path / "shared.yaml").write_text(
        _schema("shared", shared, []), encoding="utf-8"
    )
    path = tmp_path / "mart.yaml"
    path.write_text(_schema("mart", local, ["shared"]), encoding="utf-8")
    return path


def _loaded(path: pathlib.Path) -> DimensionalModel:
    return DimensionalModel.load(path, importmap=registry.importmap())


def test_an_imported_class_is_a_table_of_this_model(
    tmp_path: pathlib.Path,
) -> None:
    """It is emitted into this model's DDL, so it is checked as one.

    The alternative — skipping what another schema declares — passes a
    model whose generated file creates a fact with a foreign key to a table
    the file never creates.
    """
    path = two_files(
        tmp_path, {"DimLocal": dimension()}, {"DimShared": dimension()}
    )
    model = _loaded(path)
    assert [t.name for t in model.tables] == ["DimLocal", "DimShared"]
    assert model.table("DimShared") is not None


def test_a_finding_says_which_file_declared_the_subject(
    tmp_path: pathlib.Path,
) -> None:
    """Flag whose fault a finding is, which the output could not say.

    A shared schema's error failed every mart importing it, in a message
    naming a class that appears in none of the files the reader has open.
    """
    broken = dimension()
    del broken["attributes"]["d_id"]  # V302: no natural key
    path = two_files(tmp_path, {"DimLocal": dimension()}, {"DimShared": broken})
    found = [f for f in rules.check(_loaded(path)) if f.rule == "V302"]
    assert [f.subject for f in found] == ["DimShared"]
    assert found[0].origin == "shared.yaml"
    assert "imported from shared.yaml" in str(found[0])


def test_a_finding_against_this_file_carries_no_origin(
    tmp_path: pathlib.Path,
) -> None:
    """A model in one file reads exactly as it did.

    The common case, every transcript in the documentation, and the reason
    the origin is empty rather than always stated.
    """
    broken = dimension()
    del broken["attributes"]["d_id"]
    model = build(tmp_path, {"DimThing": broken})
    found = [f for f in rules.check(model) if f.rule == "V302"]
    assert found
    assert all(f.origin == "" for f in found)
    assert "imported from" not in str(found[0])


def test_skipping_imported_findings_keeps_every_rule_looking_at_them(
    tmp_path: pathlib.Path,
) -> None:
    """The flag drops reports, never tables.

    The distinction is the whole design. A physical name colliding with an
    imported table, a foreign key naming a class that does not exist and a
    level reaching through a key into another schema are all faults of
    *this* model that only a rule seeing both files can find.
    """
    broken = dimension()
    del broken["attributes"]["d_id"]
    path = two_files(tmp_path, {"DimLocal": dimension()}, {"DimShared": broken})
    model = _loaded(path)

    assert "V302" in {f.rule for f in rules.check(model)}
    assert not rules.check(model, skip_imported=True)
    # Still two tables, and the generator still emits both.
    assert len(model.tables) == 2


def test_a_collision_with_an_imported_table_is_reported_against_this_one(
    tmp_path: pathlib.Path,
) -> None:
    """The one rule whose subject had to move.

    V801 named the first colliding class in sorted order, which for an
    imported one is a fault filed under the schema that was there first —
    and `--skip-imported` would then hide a name this model chose. The
    message still names both.
    """
    path = two_files(
        tmp_path,
        {"Dim_Shared": dimension()},  # derives dim_shared, same as DimShared
        {"DimShared": dimension()},
    )
    model = _loaded(path)
    found = [f for f in rules.check(model) if f.rule == "V801"]
    assert [f.subject for f in found] == ["Dim_Shared"]
    assert "DimShared" in found[0].message
    assert found[0].origin == ""
    assert "V801" in {f.rule for f in rules.check(model, skip_imported=True)}


def test_the_summary_counts_what_it_did_not_report(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A silent skip is a check that quietly stopped applying.

    Said only when something was skipped, so a model in one file prints the
    line the documentation quotes.
    """
    broken = dimension()
    del broken["attributes"]["d_id"]
    path = two_files(tmp_path, {"DimLocal": dimension()}, {"DimShared": broken})

    assert cli.main(["check", str(path)]) == 1
    assert cli.main(["check", str(path), "--skip-imported"]) == 0
    out = capsys.readouterr().out
    assert "2 tables checked (1 imported, not reported)" in out


def test_generate_takes_the_same_scope_as_check(
    tmp_path: pathlib.Path,
) -> None:
    """`_blocking` promises the two commands cannot disagree.

    A flag on one of them is exactly how they would, so it is on both — and
    the imported tables are emitted either way, because a fact referencing
    a dimension the file never creates is DDL that does not run.
    """
    broken = dimension()
    del broken["attributes"]["d_id"]
    path = two_files(tmp_path, {"DimLocal": dimension()}, {"DimShared": broken})
    out = tmp_path / "out"

    assert cli.main(["generate", str(path), "--out", str(out)]) == 1
    assert not out.exists()

    assert (
        cli.main(["generate", str(path), "--out", str(out), "--skip-imported"])
        == 0
    )
    emitted = (out / "sql" / "mart.sql").read_text(encoding="utf-8")
    assert "dim_shared" in emitted
    assert "dim_local" in emitted
