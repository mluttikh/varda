"""The test suite.

Structured around what could actually go wrong rather than around the module
layout: a section per property the design depends on. The largest section is
extension validation, because those are the failures that are unfixable once
somebody has shipped a model against them.
"""

from __future__ import annotations

import importlib.metadata
import pathlib
import textwrap
from typing import TYPE_CHECKING, Any

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

RETAIL = pathlib.Path(__file__).parents[1] / "examples" / "retail.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build(tmp_path: pathlib.Path, classes: dict[str, Any]) -> DimensionalModel:
    """Write a minimal LinkML schema and load it as a model.

    Tests state only the classes they care about. Everything a rule needs to
    be *provoked* is in the class; everything else is boilerplate that would
    otherwise be repeated thirty times and read past.
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
        "imports": ["linkml:types"],
        "classes": classes,
    }
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(schema), encoding="utf-8")
    return DimensionalModel.load(path)


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


def test_example_is_clean() -> None:
    """The shipped example must pass its own rules.

    An example that does not conform is worse than no example: it is the
    first thing anybody copies.
    """
    assert rules.check(DimensionalModel.load(RETAIL)) == []


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
    assert "V103" in codes(model)


def test_v104_fact_without_grain_statement(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {"FctX": _fact(**{"varda:grain": ["d_key"]}), "DimThing": dimension()},
    )
    assert "V104" in codes(model)


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
    assert "V104" in codes(model)


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
    assert "V114" in codes(model)


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
    assert "V115" in codes(model)


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
    assert not fired & {"V103", "V104", "V114", "V115"}


def test_v105_dimension_without_surrogate_key(
    tmp_path: pathlib.Path,
) -> None:
    table = dimension()
    del table["attributes"]["d_key"]
    model = build(tmp_path, {"DimThing": table})
    assert "V105" in codes(model)


def test_v105_dimension_with_two_surrogate_keys(
    tmp_path: pathlib.Path,
) -> None:
    table = dimension()
    table["attributes"]["other_key"] = {
        "range": "integer",
        "annotations": {"varda:role": "SURROGATE_KEY"},
    }
    model = build(tmp_path, {"DimThing": table})
    found = [f for f in rules.check(model) if f.rule == "V105"]
    assert found
    assert "found 2" in found[0].message


def test_v106_dimension_without_natural_key(tmp_path: pathlib.Path) -> None:
    table = dimension()
    del table["attributes"]["d_id"]
    model = build(tmp_path, {"DimThing": table})
    assert "V106" in codes(model)


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
    assert "V107" in codes(model)


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
    assert "V108" in codes(model)


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
    assert "V109" in codes(model)


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
    assert "V110" in codes(model)


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
    assert "V111" in codes(model)


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
    assert "V112" in codes(model)


def test_v113_dimension_without_scd(tmp_path: pathlib.Path) -> None:
    model = build(
        tmp_path,
        {"DimThing": dimension(annotations={"varda:role": "DIMENSION"})},
    )
    assert "V113" in codes(model)


def test_v201_measure_without_additivity(tmp_path: pathlib.Path) -> None:
    table = dimension(annotations={"varda:role": "BRIDGE"})
    table["attributes"]["amount"] = {
        "range": "decimal",
        "annotations": {"varda:role": "MEASURE"},
    }
    model = build(tmp_path, {"BridgeX": table})
    assert "V201" in codes(model)


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
    assert "V202" in codes(model)


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
    found = [f for f in rules.check(model) if f.rule == "V203"]
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
    assert "V204" in codes(model)


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
    assert "V205" in codes(model)


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
    assert "V205" not in codes(model)


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
    assert "V205" in codes(model)


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
    assert "V206" in codes(model)


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
    assert "V206" not in codes(model)


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
    assert "V113" in codes(model)
    fired = {f.rule for f in rules.check(model, exemptions=["V113"])}
    assert "V113" not in fired


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
    """Acme raises V205 from warning to error."""
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
        found = [f for f in rules.check(model) if f.rule == "V205"]
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
            _bare("group", "grp", severity_defaults={"V205": "error"}),
            _bare("team", "tm", severity_defaults={"V205": "info"}),
        )


def test_agreeing_severity_defaults_are_fine() -> None:
    with registry.using(
        _bare("group", "grp", severity_defaults={"V205": "error"}),
        _bare("team", "tm", severity_defaults={"V205": "error"}),
    ):
        assert registry.severities()["V205"] == "error"


def test_unknown_severity_is_refused() -> None:
    with pytest.raises(ExtensionError, match="not one of"):
        activate(_bare("a", "one", severity_defaults={"V205": "loud"}))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def test_sql_is_deterministic() -> None:
    model = DimensionalModel.load(RETAIL)
    assert generate_sql(model) == generate_sql(model)


def test_sql_orders_dimensions_before_facts() -> None:
    sql = generate_sql(DimensionalModel.load(RETAIL))
    assert sql.index("dim_customer (") < sql.index("fct_sale (")


def test_sql_emits_foreign_keys() -> None:
    sql = generate_sql(DimensionalModel.load(RETAIL))
    assert (
        "FOREIGN KEY (customer_key) REFERENCES mart.dim_customer "
        "(customer_key)" in sql
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
        V205 = "info"
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    with registry.using(EXTENSION):
        assert EXTENSION.severity_defaults["V205"] == "error"
        assert registry.severities()["V205"] == "info"


def test_toml_exemptions_are_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        """
        exempt = ["V113", "V205"]
        """,
    )
    monkeypatch.setenv("VARDA_CONFIG", str(path))
    registry.reset_caches()
    assert registry.exemptions() == ["V113", "V205"]


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
    write_config(tmp_path, 'exempt = ["V113"]')
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
    assert not codes(model) & {"V116", "V117", "V118", "V119"}


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
    assert not codes(model) & {"V116", "V117", "V118", "V119"}


def test_version_counter_strategy_is_accepted(tmp_path: pathlib.Path) -> None:
    """A bare counter and no timestamps at all."""
    model = build(
        tmp_path,
        {"DimThing": versioned(v=_col("VERSION_NUMBER", "integer"))},
    )
    assert not codes(model) & {"V116", "V117", "V118", "V119"}


def test_v116_versioning_on_a_type_1_dimension(
    tmp_path: pathlib.Path,
) -> None:
    table = dimension()
    table["attributes"]["vf"] = _col("VERSION_START")
    model = build(tmp_path, {"DimThing": table})
    assert "V116" in codes(model)


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
    assert "V112" in codes(model)


def test_v117_version_end_without_a_start(tmp_path: pathlib.Path) -> None:
    model = build(tmp_path, {"DimThing": versioned(vt=_col("VERSION_END"))})
    assert "V117" in codes(model)


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
    assert "V118" in codes(model)


def test_v119_type_2_with_no_versioning_column(
    tmp_path: pathlib.Path,
) -> None:
    model = build(tmp_path, {"DimThing": versioned()})
    assert "V119" in codes(model)


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
    assert "UNIQUE (d_id, vf)" in sql
    assert "UNIQUE (d_id)" not in sql


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
    assert "V114" in codes(model)


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
    assert "V114" in codes(model)


def test_v120_scd_on_a_fact(tmp_path: pathlib.Path) -> None:
    fct = _fact(
        **{
            "varda:grain": ["d_key"],
            "varda:grain_statement": "one row per thing",
            "varda:scd": "TYPE_2",
        }
    )
    model = build(tmp_path, {"FctX": fct, "DimThing": dimension()})
    assert "V120" in codes(model)


def test_v120_fact_type_on_a_dimension(tmp_path: pathlib.Path) -> None:
    table = dimension()
    table["annotations"]["varda:fact_type"] = "TRANSACTION"
    model = build(tmp_path, {"DimThing": table})
    assert "V120" in codes(model)


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
    assert "UNIQUE (d_id, vs)" in sql
    assert "UNIQUE (d_id, vs, vn)" not in sql
