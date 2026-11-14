"""The model, as data — the read API every rule and generator walks.

A snapshot records what a model says, read once and frozen. Nothing here
holds a LinkML object, so nothing reachable from here is untyped, and
nothing changes under a reader because something else walked the schema.
The LinkML reading lives in :mod:`varda._linkml`, which builds these and is
the only module that knows LinkML exists.

What Varda *concludes* from a model — the uniqueness it implies, the order
its tables are created in, the key a foreign key lands on — is not here. It
is in :mod:`varda.analysis`, because the two change for different reasons
and carry different promises: this module changes when the vocabulary does,
and the analysis tightens whenever Varda finds a case it reads wrongly.

Construction is lenient. A snapshot is built from models that fail `check`,
because the rules need something to inspect and `--force` generates from
them. A name the model wrote that resolves to nothing is a :class:`Ref`
whose ``target`` is ``None``, and a value outside its enum leaves the typed
field ``None`` while staying visible in :attr:`Table.annotations`.

Every element is a frozen dataclass compared by identity. The graph is
cyclic — a column knows its table, a table its model — so the back
references are set once, by the reader, before anything else can see them.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    import pathlib

    from .registry import Registry
    from .vocabulary import Vocabulary

T = TypeVar("T")


# ---------------------------------------------------------------------------
# The closed vocabularies
#
# Each mirrors an enum in `profile/varda.yaml`, and a test holds the two
# equal. A `StrEnum` member compares equal to its string, so `role ==
# "FACT"` still holds; what the type adds is that a `match` ending in
# `assert_never` is a type error when a value is added and not handled.
# ---------------------------------------------------------------------------


class TableRole(StrEnum):
    """What part a class plays in the dimensional model."""

    FACT = "FACT"
    DIMENSION = "DIMENSION"
    BRIDGE = "BRIDGE"


class ColumnRole(StrEnum):
    """What part a slot plays inside its table."""

    SURROGATE_KEY = "SURROGATE_KEY"
    NATURAL_KEY = "NATURAL_KEY"
    FOREIGN_KEY = "FOREIGN_KEY"
    MEASURE = "MEASURE"
    ATTRIBUTE = "ATTRIBUTE"
    DEGENERATE_DIMENSION = "DEGENERATE_DIMENSION"
    VERSION_START = "VERSION_START"
    VERSION_END = "VERSION_END"
    IS_CURRENT = "IS_CURRENT"
    VERSION_NUMBER = "VERSION_NUMBER"


class Additivity(StrEnum):
    """How a measure may be aggregated across dimensions."""

    ADDITIVE = "ADDITIVE"
    SEMI_ADDITIVE = "SEMI_ADDITIVE"
    NON_ADDITIVE = "NON_ADDITIVE"


class SemiAdditiveAggregate(StrEnum):
    """What a semi-additive measure takes in place of the sum."""

    LAST = "LAST"
    FIRST = "FIRST"
    MIN = "MIN"
    MAX = "MAX"
    AVERAGE = "AVERAGE"


#: The aggregates that pick one member by its place in an order, and so need
#: exactly one dimension to order by.
ORDERED_AGGREGATES = frozenset(
    {SemiAdditiveAggregate.LAST, SemiAdditiveAggregate.FIRST}
)


class SlowlyChangingType(StrEnum):
    """How a dimension responds when a source attribute changes."""

    TYPE_0 = "TYPE_0"
    TYPE_1 = "TYPE_1"
    TYPE_2 = "TYPE_2"


class FactType(StrEnum):
    """The temporal shape of a fact table."""

    TRANSACTION = "TRANSACTION"
    PERIODIC_SNAPSHOT = "PERIODIC_SNAPSHOT"
    ACCUMULATING_SNAPSHOT = "ACCUMULATING_SNAPSHOT"
    FACTLESS = "FACTLESS"


#: The column roles that mark one version of a dimension row off from
#: another. Named as a set because every rule about versioning asks the same
#: question — is this column one of them.
VERSIONING_ROLES = frozenset(
    {
        ColumnRole.VERSION_START,
        ColumnRole.VERSION_END,
        ColumnRole.IS_CURRENT,
        ColumnRole.VERSION_NUMBER,
    }
)

#: The physical type facets a column may declare, and the ranges each one
#: means anything on. A facet parameterizes the emitted SQL type — the 80 in
#: `VARCHAR(80)` — so it is legal exactly where the type it parameterizes is.
#:
#: Keyed by facet rather than by range so that V803 can name the ranges a
#: misplaced facet would have been legal on, which is the half of the message
#: that says what to do about it.
FACETS: dict[str, frozenset[str]] = {
    "max_length": frozenset({"string", "uri", "uriorcurie", "ncname"}),
    "precision": frozenset({"decimal"}),
    "scale": frozenset({"decimal"}),
}

#: The smallest value each facet may take. Scale alone may be zero, because
#: `NUMERIC(18, 0)` is a whole number stored as a decimal — a declaration
#: somebody means, and the default a database picks when no scale is given.
FACET_MINIMUM: dict[str, int] = {
    "max_length": 1,
    "precision": 1,
    "scale": 0,
}


# ---------------------------------------------------------------------------
# Annotations, and names that may not resolve
# ---------------------------------------------------------------------------


class Annotations(Mapping[str, Any]):
    """Every annotation on one element, by full tag, as it was written.

    Across every prefix, because the rules that check annotations — and the
    extensions that read their own — need what the model said rather than
    what Varda made of it. ``of`` narrows to one namespace with the prefix
    stripped, which is how an extension reads its vocabulary::

        table.annotations.of("acme").get("cost_center")

    The match on a prefix is exact. An unprefixed ``role:`` annotation is
    nobody's, not everybody's, and reading it as Varda's is how one party's
    vocabulary silently consumes another's.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        """Hold a copy of ``values``, so the caller cannot change it later."""
        self._values: dict[str, Any] = dict(values or {})

    def __getitem__(self, tag: str) -> Any:
        """Return one annotation's raw value by full tag."""
        return self._values[tag]

    def __iter__(self) -> Iterator[str]:
        """Iterate the full tags, in the order they were written."""
        return iter(self._values)

    def __len__(self) -> int:
        """Count the annotations, across every prefix."""
        return len(self._values)

    def __repr__(self) -> str:
        """Render like the mapping it is."""
        return f"Annotations({self._values!r})"

    def of(self, prefix: str) -> Mapping[str, Any]:
        """Return one namespace's annotations, prefix stripped."""
        tag = f"{prefix}:"
        n = len(tag)
        return {k[n:]: v for k, v in self._values.items() if k.startswith(tag)}

    def text(self, tag: str) -> str | None:
        """Read one annotation as a string, or ``None`` where it is absent.

        A value arrives as whatever the YAML parser made of it — ``16`` is
        an int, ``true`` a bool — and a caller wanting the text the model
        wrote wants it the same way every time.
        """
        value = self._values.get(tag)
        return None if value is None else str(value)


@dataclass(frozen=True)
class Ref(Generic[T]):
    """A name the model wrote, and what it resolved to — if anything.

    ``target`` is ``None`` when the name resolves to nothing, which is a
    rule's finding to report rather than an error here. A generator never
    reads it directly: :mod:`varda.analysis` answers for the resolved case,
    so what a half-broken model looks like to a generator is decided once.
    """

    name: str
    target: T | None

    def __str__(self) -> str:
        """Render as the name the model wrote."""
        return self.name


# ---------------------------------------------------------------------------
# What the model says an element means
#
# LinkML already has the words for this — `class_uri`, `slot_uri`, the five
# SKOS-shaped mapping slots, `unit`, an enum value's `meaning` — and Varda
# adds none of its own. What it adds is that they arrive: a rule can require
# them and a generator can emit them, where before a model could say all of
# it and nothing downstream could tell.
#
# Every IRI is kept as it was written, a CURIE or a full IRI, because the
# spelling is what the model says and what a finding should quote.
# :meth:`Model.expand` turns one into the other.
# ---------------------------------------------------------------------------


def expand_iri(uri: str, prefixes: Mapping[str, str]) -> str | None:
    """Expand a CURIE through a prefix map; see :meth:`Model.expand`."""
    prefix, sep, local = uri.partition(":")
    if not sep:
        return None
    namespace = prefixes.get(prefix)
    if namespace is not None:
        return namespace + local
    # `https://…` and `urn:…` split on their first colon too, and neither
    # is a CURIE: no prefix is named `https`, and what follows the scheme of
    # a full IRI is never a bare local name.
    if local.startswith("//") or prefix.lower() == "urn":
        return uri
    return None


#: The kinds of mapping, in the order :meth:`Mappings.items` walks them.
MAPPING_KINDS: tuple[str, ...] = (
    "exact",
    "close",
    "related",
    "narrow",
    "broad",
    "unqualified",
)


@dataclass(frozen=True)
class Mappings:
    """What an element is said to correspond to elsewhere, by how closely.

    LinkML's mapping slots, one field each and named for the SKOS property
    each one is. ``unqualified`` is the plain ``mappings:`` slot, which says
    a correspondence exists and not which kind.
    """

    exact: tuple[str, ...] = ()
    close: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    narrow: tuple[str, ...] = ()
    broad: tuple[str, ...] = ()
    unqualified: tuple[str, ...] = ()

    def items(self) -> Iterator[tuple[str, str]]:
        """Walk every mapping as ``(kind, target)``, closest kind first."""
        for kind in MAPPING_KINDS:
            for target in getattr(self, kind):
                yield kind, target

    def __bool__(self) -> bool:
        """Flag whether the element maps to anything at all."""
        return any(getattr(self, kind) for kind in MAPPING_KINDS)


@dataclass(frozen=True)
class Unit:
    """LinkML's ``unit``, every part of it the model gave.

    One unit may be written as a symbol, a UCUM code, an abbreviation or a
    name, and a model may give any subset. :attr:`label` is the one a reader
    would recognize; the rest is here because a label cannot be joined on,
    and ``exact_mappings`` is where a model names the unit in QUDT.
    """

    symbol: str | None = None
    ucum_code: str | None = None
    abbreviation: str | None = None
    descriptive_name: str | None = None
    iec61360code: str | None = None
    has_quantity_kind: str | None = None
    derivation: str | None = None
    exact_mappings: tuple[str, ...] = ()

    @property
    def label(self) -> str | None:
        """The unit as one label, tried in the order a reader would know it."""
        for value in (
            self.symbol,
            self.ucum_code,
            self.abbreviation,
            self.descriptive_name,
        ):
            if value is not None and value.strip():
                return value
        return None


@dataclass(frozen=True)
class PermissibleValue:
    """One value of an enum, and the concept it stands for if it names one."""

    text: str
    description: str
    title: str | None
    #: LinkML's ``meaning``, as written.
    meaning: str | None
    mappings: Mappings


@dataclass(frozen=True)
class Enumeration:
    """An enum the schema declares or imports.

    Not a column type — V804 still refuses one as a range — but a value set
    is where a model most often already holds its links to a controlled
    vocabulary, and a hierarchy level that names a SKOS scheme will want to
    point at one.
    """

    name: str
    description: str
    #: LinkML's ``enum_uri``, as written, or ``None`` when none was.
    uri: str | None
    values: tuple[PermissibleValue, ...]

    def value(self, text: str) -> PermissibleValue | None:
        """Look up one permissible value by its text."""
        for found in self.values:
            if found.text == text:
                return found
        return None


# ---------------------------------------------------------------------------
# The elements
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Column:
    """One slot of a table class, read through the active profiles."""

    name: str
    physical: str
    #: Whether ``physical`` was declared with ``varda:physical_name`` rather
    #: than derived from the slot name. V802 says which, because the
    #: realistic collision is a declared name meeting a derived one.
    physical_declared: bool
    role: ColumnRole | None
    #: The range as written, ``string`` when none was.
    range: str
    #: The range, then every type it is declared in terms of, nearest first:
    #: `uuid` gives ``("uuid", "string")``. A range naming nothing gives
    #: itself alone, which is V804's to report.
    type_chain: tuple[str, ...]
    required: bool
    references: Ref[Table] | None
    additivity: Additivity | None
    #: The foreign keys ``varda:semi_additive_over`` names, in the order
    #: written; one name written alone is one of them.
    semi_additive_keys: tuple[Ref[Column], ...]
    semi_additive_aggregate: SemiAdditiveAggregate | None
    #: LinkML's own ``unit``, every part the model gave. :attr:`unit` is
    #: the same thing as one label.
    unit_of_measure: Unit | None
    #: The facets, each ``None`` unless declared, legal on this column's
    #: type and a whole number at or above its floor. V803 reports the rest.
    max_length: int | None
    precision: int | None
    scale: int | None
    description: str
    #: LinkML's ``title`` and ``aliases``: what a person calls the column.
    title: str | None
    aliases: tuple[str, ...]
    #: LinkML's ``slot_uri`` as written — a CURIE or a full IRI — or
    #: ``None`` when the model declared none. Not defaulted: the IRI LinkML
    #: would mint from the slot name is the same for a `date_key` in every
    #: table, and which IRI an undeclared column gets is a generator's
    #: decision rather than something the model said.
    uri: str | None
    mappings: Mappings
    instantiates: tuple[str, ...]
    annotations: Annotations
    table: Table = field(init=False, repr=False)

    @property
    def semi_additive_over(self) -> Ref[Column] | None:
        """The first of :attr:`semi_additive_keys`, or ``None``.

        Kept for readers written when a measure named one key at most.
        """
        keys = self.semi_additive_keys
        return keys[0] if keys else None

    @property
    def unit(self) -> str | None:
        """LinkML's own ``unit``, as the label a reader would recognize."""
        found = self.unit_of_measure
        return None if found is None else found.label

    def parameterizes(self, facet: str) -> bool:
        """Flag whether this column's type is one ``facet`` says anything on.

        Asked of the whole type chain, so a column ranged on a schema's own
        ``money: {typeof: decimal}`` may carry a precision.
        """
        return bool(FACETS[facet] & {t.lower() for t in self.type_chain})

    @property
    def is_measure(self) -> bool:
        return self.role == ColumnRole.MEASURE

    @property
    def is_key(self) -> bool:
        return self.role in {ColumnRole.SURROGATE_KEY, ColumnRole.FOREIGN_KEY}

    def __str__(self) -> str:
        """Render as ``Table.column``, which is how findings name a column."""
        return f"{self.table.name}.{self.name}"


@dataclass(frozen=True, eq=False)
class Level:
    """One step of a drill path.

    A level names either a column of the hierarchy's own table, or a column
    of a dimension reached through one of that table's foreign keys —
    ``country_key.country_name``. ``via`` is that foreign key, or ``None``
    for a plain level; ``column`` is the column that names the level,
    wherever it lives. Either is ``None`` when it names nothing, which is
    V601's to report.

    ``key`` is the key the model declared for this level, looked up in
    whichever table supplied ``column``, or ``None`` when it declared none.
    What identifies a level that declares no key is Varda's reading of it,
    and is :func:`varda.analysis.level_key`.
    """

    spec: str
    via: Column | None
    column: Column | None
    key: Ref[Column] | None
    hierarchy: Hierarchy = field(init=False, repr=False)

    @property
    def is_reference(self) -> bool:
        """Flag whether this level reaches through a foreign key."""
        return "." in self.spec

    def __str__(self) -> str:
        """Render as it was written, which is how findings name a level."""
        return self.spec


@dataclass(frozen=True, eq=False)
class Hierarchy:
    """One named drill path, its levels from least to most granular."""

    name: str
    description: str
    levels: tuple[Level, ...]
    table: Table = field(init=False, repr=False)

    @property
    def specs(self) -> tuple[str, ...]:
        """The levels as written, in declared order."""
        return tuple(level.spec for level in self.levels)

    @property
    def level_columns(self) -> tuple[Column, ...]:
        """The columns that name the levels, skipping what does not resolve."""
        return tuple(lv.column for lv in self.levels if lv.column is not None)

    def __str__(self) -> str:
        """Render as ``Table.hierarchy``, which is how findings name one."""
        return f"{self.table.name}.{self.name}"


@dataclass(frozen=True, eq=False)
class UniqueKey:
    """One of LinkML's own ``unique_keys``, read rather than restated."""

    name: str
    declared: tuple[Ref[Column], ...]
    description: str
    table: Table = field(init=False, repr=False)

    @property
    def columns(self) -> tuple[Column, ...]:
        """The columns the key names, skipping what does not resolve."""
        return tuple(r.target for r in self.declared if r.target is not None)

    def __str__(self) -> str:
        """Render as ``Table.key``, which is how findings name one."""
        return f"{self.table.name}.{self.name}"


@dataclass(frozen=True, eq=False)
class Table:
    """One class of the model that carries Varda annotations."""

    name: str
    physical: str
    physical_declared: bool
    role: TableRole | None
    fact_type: FactType | None
    scd: SlowlyChangingType | None
    grain: tuple[Ref[Column], ...]
    grain_statement: str | None
    hierarchies: tuple[Hierarchy, ...]
    #: Walked up the inheritance chain, which LinkML does not do for keys
    #: although it does for columns; sorted by name.
    unique_keys: tuple[UniqueKey, ...]
    #: Every column, inherited ones included, in declaration order.
    columns: tuple[Column, ...]
    description: str
    #: LinkML's ``title`` and ``aliases``: what a person calls the table.
    title: str | None
    aliases: tuple[str, ...]
    #: LinkML's ``class_uri`` as written, or ``None`` when the model
    #: declared none. See :attr:`Column.uri` for why it is not defaulted.
    uri: str | None
    mappings: Mappings
    #: The file this class was declared in, when it arrived through
    #: ``imports:``; ``None`` when this model declared it. Named by file
    #: rather than by the schema's IRI, because the file is what somebody
    #: opens.
    origin: str | None
    instantiates: tuple[str, ...]
    annotations: Annotations
    model: Model = field(init=False, repr=False)
    _by_name: dict[str, Column] = field(init=False, repr=False)

    def column(self, name: str) -> Column | None:
        """Look up one column by name."""
        return self._by_name.get(name)

    @property
    def is_imported(self) -> bool:
        return self.origin is not None

    @property
    def is_fact(self) -> bool:
        return self.role == TableRole.FACT

    @property
    def is_dimension(self) -> bool:
        return self.role == TableRole.DIMENSION

    @property
    def is_bridge(self) -> bool:
        return self.role == TableRole.BRIDGE

    @property
    def grain_columns(self) -> tuple[Column, ...]:
        """The grain's columns, skipping names that resolve to nothing."""
        return tuple(r.target for r in self.grain if r.target is not None)

    @property
    def surrogate_keys(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.SURROGATE_KEY)

    @property
    def natural_keys(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.NATURAL_KEY)

    @property
    def foreign_keys(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.FOREIGN_KEY)

    @property
    def measures(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.MEASURE)

    @property
    def attributes(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.ATTRIBUTE)

    @property
    def degenerates(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.DEGENERATE_DIMENSION)

    @property
    def version_starts(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.VERSION_START)

    @property
    def version_ends(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.VERSION_END)

    @property
    def current_flags(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.IS_CURRENT)

    @property
    def version_numbers(self) -> tuple[Column, ...]:
        return self.by_role(ColumnRole.VERSION_NUMBER)

    @property
    def versioning(self) -> tuple[Column, ...]:
        """Every column that discriminates one version of a row from another.

        Type 2 is defined by keeping a row per change, not by how the current
        one is found, and the field uses at least three mechanisms to find
        it. Callers that need to know *whether* a dimension versions ask
        this; callers that need to know *how* ask for the specific role.
        """
        return tuple(c for c in self.columns if c.role in VERSIONING_ROLES)

    def by_role(self, role: ColumnRole) -> tuple[Column, ...]:
        """Every column playing ``role``, in declaration order."""
        return tuple(c for c in self.columns if c.role == role)

    def __str__(self) -> str:
        """Render as the class name, which is how findings name a table."""
        return self.name


@dataclass(frozen=True, eq=False)
class Model:
    """A LinkML schema, read as a dimensional model.

    Only classes carrying Varda annotations are tables. A schema may hold
    anything else it likes alongside them — that is the point of being a
    profile rather than a format.

    ``registry`` is the extension set the model was read under. A model's
    annotations mean something only relative to the profiles that declare
    them, so the two travel together: the rules that check annotations read
    :attr:`vocabulary`, and :func:`varda.api.check` runs the registry's rules
    unless it is handed another.
    """

    source: pathlib.Path
    schema_id: str
    #: Every annotated class, imported ones included, sorted by name.
    tables: tuple[Table, ...]
    #: The prefixes the schema itself declares — V003 does not warn about
    #: an annotation whose prefix the model has said it knows.
    declared_prefixes: frozenset[str]
    #: Every prefix a CURIE in this model can be written with, and the
    #: namespace each expands to: the schema's own and those of everything
    #: it imports, because an imported table's ``class_uri`` is written
    #: against the prefixes of the file that declared it. Where two files
    #: disagree about a prefix, this schema's reading wins.
    prefixes: Mapping[str, str]
    #: Every enum the schema declares or imports, sorted by name.
    enums: tuple[Enumeration, ...]
    #: Every type, class and enum the schema can resolve a range to, its
    #: imports included. V804 tells the three apart.
    declared_types: frozenset[str]
    declared_classes: frozenset[str]
    declared_enums: frozenset[str]
    registry: Registry
    _by_name: dict[str, Table] = field(init=False, repr=False)

    @property
    def vocabulary(self) -> Vocabulary:
        """What the active profiles declare, which annotations are read by."""
        return self.registry.vocabulary

    def table(self, name: str) -> Table | None:
        """Look up one table by class name."""
        return self._by_name.get(name)

    def enum(self, name: str) -> Enumeration | None:
        """Look up one enum by name."""
        for found in self.enums:
            if found.name == name:
                return found
        return None

    def expand(self, uri: str) -> str | None:
        """Turn an IRI the model wrote into a full one, or ``None``.

        A CURIE is expanded through :attr:`prefixes`, and one whose prefix
        the model never declared gives ``None`` — a name that resolves to
        nothing, which is a rule's finding to report, exactly as a
        :class:`Ref` without a target is. A full IRI is returned as it is.
        """
        return expand_iri(uri, self.prefixes)

    @property
    def facts(self) -> tuple[Table, ...]:
        return tuple(t for t in self.tables if t.is_fact)

    @property
    def dimensions(self) -> tuple[Table, ...]:
        return tuple(t for t in self.tables if t.is_dimension)

    @property
    def bridges(self) -> tuple[Table, ...]:
        return tuple(t for t in self.tables if t.is_bridge)


#: Anything a finding may be about. Each renders the way a reader finds it,
#: and every one but a table renders as `Table.name` — which is what lets a
#: report say which file a finding belongs to without asking the rule.
Element = Table | Column | Hierarchy | UniqueKey


def table_of(element: Element) -> Table:
    """Name the table an element belongs to."""
    return element if isinstance(element, Table) else element.table
