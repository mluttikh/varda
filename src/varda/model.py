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
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

from linkml_runtime.utils.schemaview import SchemaView

from .anns import get, is_model_object

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
        return get(self.slot, "unit")

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
        return self.role == "measure"

    @property
    def is_key(self) -> bool:
        return self.role in {"surrogate_key", "foreign_key"}

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
    def grain(self) -> str | None:
        return get(self.cls, "grain")

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
        return self.role == "fact"

    @property
    def is_dimension(self) -> bool:
        return self.role == "dimension"

    @property
    def is_bridge(self) -> bool:
        return self.role == "bridge"

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
        return self._by_role("surrogate_key")

    @property
    def natural_keys(self) -> tuple[Column, ...]:
        return self._by_role("natural_key")

    @property
    def foreign_keys(self) -> tuple[Column, ...]:
        return self._by_role("foreign_key")

    @property
    def measures(self) -> tuple[Column, ...]:
        return self._by_role("measure")

    @property
    def attributes(self) -> tuple[Column, ...]:
        return self._by_role("attribute")

    @property
    def degenerates(self) -> tuple[Column, ...]:
        return self._by_role("degenerate_dimension")

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
