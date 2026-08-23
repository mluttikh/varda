"""The in-memory view of a dimensional model.

Everything downstream — validation, generation — reads this module rather than
touching ``SchemaView`` directly. That indirection is worth its cost: it is the
layer at which "a fact table" and "a foreign key" exist as concepts, and it is
where a change of authoring format would be absorbed.

It is also the typed boundary. LinkML ships no ``py.typed``, so everything
coming out of ``SchemaView`` is ``Any``. That is not a defect to suppress and
forget — it is the edge of the typed world, and this module is the wall built
along it. Everything returned from here is a concrete type, so nothing
downstream has to know the source was untyped.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any

from linkml_runtime.utils.schemaview import SchemaView

from .anns import get, get_list, is_model_object
from .anns import raw as raw_ann

if TYPE_CHECKING:
    from linkml_runtime.linkml_model.meta import (
        ClassDefinition,
        SlotDefinition,
    )

PROFILE = pathlib.Path(__file__).parent / "profile" / "varda.yaml"

_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def physical_name(logical: str) -> str:
    """Derive a physical name from a class or slot name.

    ``DimCustomer`` becomes ``dim_customer``; a name already in snake case is
    returned unchanged. Deliberately mechanical: a model that wants a name
    this does not produce says so with ``varda:physical_name`` rather than
    being accommodated by a cleverer rule here, because every extra rule is
    one more thing a reader has to know to predict the output.
    """
    return _BOUNDARY.sub("_", logical).lower()


#: The column roles that mark one version of a dimension row off from
#: another. Named as a set because every rule about versioning asks the same
#: question — is this column one of them — and a set that lives in one place
#: cannot drift from the profile that declares them.
VERSIONING_ROLES = frozenset(
    {"VERSION_START", "VERSION_END", "IS_CURRENT", "VERSION_NUMBER"}
)


def _level_spec(entry: Any) -> tuple[str, str]:
    """Read one level entry as its spec and its declared key names.

    A level is written as a bare column name wherever that column both names
    and identifies the level, which is the common case. The mapping form
    — ``{column: product_name, key: sku}`` — is for a level whose name is not
    what tells its members apart.
    """
    if isinstance(entry, Mapping):
        return str(entry.get("column") or ""), str(entry.get("key") or "")
    return str(entry), ""


@dataclass(frozen=True)
class Level:
    """One step of a drill path, resolved against the model.

    A level names either a column of the table its hierarchy is declared on,
    or a column of a dimension reached through one of that table's foreign
    keys — ``country_key.country_name``. The second form is what a snowflake
    needs: when the coarser levels are their own tables, the only thing on
    the near side is the key, and a path of keys reads as integers.

    ``column`` is the column that names the level to a reader, wherever it
    lives. ``via`` is the foreign key it was reached through, or ``None`` for
    a plain level. Either may be ``None`` when the level names something that
    does not exist, which is V121's finding to report rather than an error
    here.

    ``key`` is what tells one member of this level from another *under the
    same parent*, which is a different question from what names it. It is the
    foreign key for a reference level and the naming column otherwise, and a
    model declares one only when neither is right — a level showing
    `product_name` but identified by `sku`.

    ``identity`` is what tells one member from every other: this level's key
    preceded by the key of every coarser level. `city_name` holds
    "Springfield" for cities in three states, and country, state and city
    together hold one of them. That path is what a hierarchy already asserts,
    so it is derived rather than written down.
    """

    spec: str
    via: Column | None
    column: Column | None
    key: Column | None
    identity: tuple[Column, ...]
    declared_key: str

    @property
    def is_reference(self) -> bool:
        """Flag whether this level reaches through a foreign key."""
        return "." in self.spec

    def __str__(self) -> str:
        """Render as it was written, which is how findings name a level."""
        return self.spec


@dataclass(frozen=True)
class Hierarchy:
    """One named drill path over a dimension's columns.

    ``levels`` runs from least to most granular, which is the order every
    system that models hierarchies uses and the order a reader drills in.
    """

    name: str
    #: One ``(spec, declared key name)`` pair per level, in declared order.
    declared: tuple[tuple[str, str], ...]
    table: Table

    @property
    def levels(self) -> tuple[str, ...]:
        """The level specs, as written, in declared order."""
        return tuple(spec for spec, _ in self.declared)

    @property
    def resolved(self) -> tuple[Level, ...]:
        """Resolve every level, in declared order.

        Resolution never fails: a level naming nothing resolves to a
        ``Level`` whose parts are ``None``. The rules report those, and a
        generator running on a model that has not passed `check` wants a
        partial answer rather than an exception from a property access.
        """
        out: list[Level] = []
        path: tuple[Column, ...] = ()
        for spec, declared_key in self.declared:
            level = self._resolve(spec, declared_key, path)
            path = level.identity
            out.append(level)
        return tuple(out)

    def _resolve(
        self, spec: str, declared_key: str, ancestors: tuple[Column, ...]
    ) -> Level:
        """Resolve one level, plain or reached through a foreign key."""
        head, dot, tail = spec.partition(".")
        near = self.table.column(head)
        column = near
        via = None
        if dot:
            via = near
            column = None
            if near is not None and near.references:
                found = self.table.model.table(near.references)
                column = found.column(tail) if found is not None else None
        if declared_key:
            key = self.table.column(declared_key)
        else:
            key = via if dot else column
        return Level(
            spec=spec,
            via=via,
            column=column,
            key=key,
            identity=(*ancestors, key) if key is not None else ancestors,
            declared_key=declared_key,
        )

    @property
    def level_columns(self) -> tuple[Column, ...]:
        """The columns that name the levels, skipping what does not resolve."""
        return tuple(lv.column for lv in self.resolved if lv.column is not None)

    def __str__(self) -> str:
        """Render as ``Table.hierarchy``, which is how findings name one."""
        return f"{self.table.name}.{self.name}"


@dataclass(frozen=True)
class Column:
    """One slot of a table class, read through the profile."""

    name: str
    slot: SlotDefinition
    table: Table

    @property
    def role(self) -> str | None:
        return get(self.slot, "role")

    @property
    def references(self) -> str | None:
        return get(self.slot, "references")

    @property
    def additivity(self) -> str | None:
        return get(self.slot, "additivity")

    @property
    def semi_additive_over(self) -> str | None:
        return get(self.slot, "semi_additive_over")

    @property
    def unit(self) -> str | None:
        """The unit of measurement, as a short label.

        Units are LinkML's own ``unit``, not a Varda annotation. It holds a
        ``UnitOfMeasure`` rather than a string, so one unit may be written as
        a symbol, a UCUM code, an abbreviation or a name, and a model may
        give any subset of those. Callers want one label, so the fields are
        tried in the order a reader would recognize them.
        """
        unit = getattr(self.slot, "unit", None)
        if unit is None:
            return None
        names = ("symbol", "ucum_code", "abbreviation", "descriptive_name")
        for field in names:
            value = getattr(unit, field, None)
            if value is not None and str(value).strip():
                return str(value)
        return None

    @property
    def physical(self) -> str:
        return get(self.slot, "physical_name") or physical_name(self.name)

    @property
    def description(self) -> str:
        return (self.slot.description or "").strip()

    @property
    def range(self) -> str:
        return str(self.slot.range or "string")

    @property
    def required(self) -> bool:
        return bool(self.slot.required)

    @property
    def is_measure(self) -> bool:
        return self.role == "MEASURE"

    @property
    def is_key(self) -> bool:
        return self.role in {"SURROGATE_KEY", "FOREIGN_KEY"}

    def __str__(self) -> str:
        """Render as ``Table.column``, which is how findings name a column."""
        return f"{self.table.name}.{self.name}"


@dataclass(frozen=True)
class Table:
    """One class of the domain model that carries Varda annotations."""

    name: str
    cls: ClassDefinition
    model: DimensionalModel

    @property
    def role(self) -> str | None:
        return get(self.cls, "role")

    @property
    def grain(self) -> tuple[str, ...]:
        """The columns at which rows are unique, in declared order."""
        return get_list(self.cls, "grain")

    @property
    def grain_statement(self) -> str | None:
        return get(self.cls, "grain_statement")

    @property
    def grain_columns(self) -> tuple[Column, ...]:
        """Resolve the declared grain to columns, skipping unknown names.

        Rules report the missing names; every other caller wants the columns
        it can actually work with, so resolution drops what it cannot find
        rather than raising. A generator running on a model that failed
        `check` is already in undefined territory, and a partial grain is a
        better failure than an exception from a property access.
        """
        found = (self.column(n) for n in self.grain)
        return tuple(c for c in found if c is not None)

    @property
    def hierarchies(self) -> tuple[Hierarchy, ...]:
        """The declared drill paths, in declared order.

        Reads a structured annotation, so this is where the untyped list of
        mappings LinkML hands back becomes concrete. Entries that are not
        mappings are dropped rather than raising: a malformed hierarchy is
        V121's finding to report, and a property access is the wrong place to
        fail on a model that has not been checked yet.
        """
        raw = raw_ann(self.cls, "hierarchies")
        if raw is None:
            return ()
        entries = raw if isinstance(raw, list) else [raw]
        out: list[Hierarchy] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            levels = entry.get("levels")
            if isinstance(levels, (str, bytes, Mapping)):
                levels = [levels]
            out.append(
                Hierarchy(
                    name=str(entry.get("name") or ""),
                    declared=tuple(_level_spec(v) for v in levels or ()),
                    table=self,
                )
            )
        return tuple(out)

    @property
    def fact_type(self) -> str | None:
        return get(self.cls, "fact_type")

    @property
    def scd(self) -> str | None:
        return get(self.cls, "scd")

    @property
    def physical(self) -> str:
        return get(self.cls, "physical_name") or physical_name(self.name)

    @property
    def description(self) -> str:
        return (self.cls.description or "").strip()

    @property
    def is_fact(self) -> bool:
        return self.role == "FACT"

    @property
    def is_dimension(self) -> bool:
        return self.role == "DIMENSION"

    @property
    def is_bridge(self) -> bool:
        return self.role == "BRIDGE"

    @cached_property
    def columns(self) -> tuple[Column, ...]:
        """Every column, in declaration order.

        Read through ``class_induced_slots`` rather than ``cls.attributes``,
        so a table that inherits columns from a mixin or a parent class sees
        them. A model that uses inheritance and a validator that does not
        understand it produce disagreements nobody can debug.
        """
        induced = self.model.view.class_induced_slots(self.name)
        return tuple(
            Column(name=str(s.name), slot=s, table=self) for s in induced
        )

    def column(self, name: str) -> Column | None:
        """Look up one column by name."""
        return next((c for c in self.columns if c.name == name), None)

    @property
    def surrogate_keys(self) -> tuple[Column, ...]:
        return self._by_role("SURROGATE_KEY")

    @property
    def natural_keys(self) -> tuple[Column, ...]:
        return self._by_role("NATURAL_KEY")

    @property
    def foreign_keys(self) -> tuple[Column, ...]:
        return self._by_role("FOREIGN_KEY")

    @property
    def measures(self) -> tuple[Column, ...]:
        return self._by_role("MEASURE")

    @property
    def attributes(self) -> tuple[Column, ...]:
        return self._by_role("ATTRIBUTE")

    @property
    def degenerates(self) -> tuple[Column, ...]:
        return self._by_role("DEGENERATE_DIMENSION")

    @property
    def version_starts(self) -> tuple[Column, ...]:
        return self._by_role("VERSION_START")

    @property
    def version_ends(self) -> tuple[Column, ...]:
        return self._by_role("VERSION_END")

    @property
    def current_flags(self) -> tuple[Column, ...]:
        return self._by_role("IS_CURRENT")

    @property
    def version_numbers(self) -> tuple[Column, ...]:
        return self._by_role("VERSION_NUMBER")

    @property
    def versioning(self) -> tuple[Column, ...]:
        """Every column that discriminates one version of a row from another.

        Type 2 is defined by keeping a row per change, not by how the current
        one is found, and the field uses at least three mechanisms to find
        it: a closed period, a start plus a flag, or a bare counter. Callers
        that need to know *whether* a dimension versions ask this; callers
        that need to know *how* ask for the specific role.
        """
        return tuple(c for c in self.columns if c.role in VERSIONING_ROLES)

    def _by_role(self, role: str) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.role == role)

    def __str__(self) -> str:
        """Render as the class name, which is how findings name a table."""
        return self.name


@dataclass(frozen=True)
class DimensionalModel:
    """A LinkML schema, read as a dimensional model.

    Only classes carrying Varda annotations are tables. A schema may hold
    anything else it likes alongside them — that is the point of being a
    profile rather than a format.
    """

    view: SchemaView
    source: pathlib.Path

    @classmethod
    def load(
        cls,
        path: str | pathlib.Path,
        importmap: dict[str, str] | None = None,
    ) -> DimensionalModel:
        """Load a domain model from a LinkML schema file.

        ``importmap`` resolves symbolic imports — it is how a model writes
        ``imports: - varda`` without knowing where the installed profile
        lives on this machine. The registry builds it; see
        :func:`varda.registry.importmap`.
        """
        source = pathlib.Path(path)
        view = SchemaView(str(source), importmap=importmap)
        return cls(view=view, source=source)

    @cached_property
    def tables(self) -> tuple[Table, ...]:
        """Every annotated class, sorted by name.

        Sorted rather than in declaration order because this drives generated
        output, and output that reorders when someone moves a class in the
        source produces a diff that is entirely noise.
        """
        out = [
            Table(name=str(name), cls=c, model=self)
            for name, c in self.view.all_classes().items()
            if is_model_object(c)
        ]
        return tuple(sorted(out, key=lambda t: t.name))

    def table(self, name: str) -> Table | None:
        """Look up one table by class name."""
        return next((t for t in self.tables if t.name == name), None)

    @property
    def facts(self) -> tuple[Table, ...]:
        return tuple(t for t in self.tables if t.is_fact)

    @property
    def dimensions(self) -> tuple[Table, ...]:
        return tuple(t for t in self.tables if t.is_dimension)

    @property
    def bridges(self) -> tuple[Table, ...]:
        return tuple(t for t in self.tables if t.is_bridge)
