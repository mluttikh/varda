"""The test suite.

Structured around what could actually go wrong rather than around the module
layout: a section per property the design depends on. The largest section is
extension validation, because those are the failures that are unfixable once
somebody has shipped a model against them.
"""

from __future__ import annotations

import decimal
import importlib.metadata
import pathlib
import textwrap
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
import yaml

from varda import __version__, cli, registry, rules
from varda.ext import Extension, ExtensionError, Generator
from varda.gen_docs import generate as generate_docs
from varda.gen_sql import GenerationError
from varda.gen_sql import generate as generate_sql
from varda.model import DimensionalModel, physical_name
from varda.rules import RuleSet

if TYPE_CHECKING:
    from collections.abc import Iterator

EXAMPLES = pathlib.Path(__file__).parents[1] / "examples"
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
            "d_id": {"annotations": {"varda:role": "NATURAL_KEY"}},
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
    sql = generate_sql(DimensionalModel.load(RETAIL))
    assert max(len(line) for line in sql.splitlines()) <= 80


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


# --- hierarchies -----------------------------------------------------------


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


# --- bridges and structured annotations -------------------------------------


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


# --- unique keys ------------------------------------------------------------


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
    second as a duplicate. Varda emits dialect-neutral DDL and cannot know
    which it will meet, so the pair is refused rather than left to whichever
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


# --- type facets ------------------------------------------------------------


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
