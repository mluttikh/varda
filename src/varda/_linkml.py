"""Reading LinkML — the one module that knows it exists.

Everything coming out of ``SchemaView`` is ``Any``: LinkML ships no
``py.typed``. This module is the wall built along that boundary. It reads a
schema once, turns what it finds into :mod:`varda.snapshot`'s frozen types,
and hands back nothing LinkML made — so nothing downstream, and nothing an
extension can reach, is untyped.

Internal. Nothing outside the package imports it, and its functions may
change in any release.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterable, Mapping
from functools import cache
from typing import TYPE_CHECKING, Any, TypeVar

from jsonasobj2 import JsonObj, as_dict
from jsonasobj2 import items as _ja_items
from linkml_runtime.utils.schemaview import SchemaView

from .analysis import physical_name
from .snapshot import (
    FACET_MINIMUM,
    FACETS,
    Additivity,
    Annotations,
    Column,
    ColumnRole,
    FactType,
    Hierarchy,
    Level,
    Model,
    Ref,
    SlowlyChangingType,
    Table,
    TableRole,
    UniqueKey,
)

if TYPE_CHECKING:
    from enum import StrEnum

    from .registry import Registry

E = TypeVar("E", bound="StrEnum")


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def annotations_of(obj: Any) -> dict[str, Any]:
    """Return an object's annotations as a plain ``{tag: value}`` dict.

    LinkML stores annotations as ``JsonObj`` in some code paths and plain
    dicts in others, and the two do not share an interface. Every read of an
    annotation goes through here, so that is handled in exactly one place.
    """
    declared = getattr(obj, "annotations", None)
    if not declared:
        return {}
    out: dict[str, Any] = {}
    for tag, ann in _ja_items(declared):
        value = (
            ann.get("value")
            if isinstance(ann, dict)
            else getattr(ann, "value", ann)
        )
        out[str(tag)] = _plain(value)
    return out


def _plain(value: Any) -> Any:
    """Rebuild a value LinkML parsed out of plain Python types.

    What arrives is LinkML's: an `extended_str` or `extended_int` carrying
    the YAML position it was read from, or a `JsonObj` where a mapping was
    written. Each passes for the builtin it extends until something checks
    its type or its module, and handing one on would put LinkML back on the
    far side of the wall this module is.
    """
    if isinstance(value, JsonObj):
        value = as_dict(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    for builtin in (str, int, float):
        if isinstance(value, builtin):
            return builtin(value)
    return value


def text(obj: Any, tag: str) -> str | None:
    """Read one annotation by full tag as a string, or ``None``."""
    value = annotations_of(obj).get(tag)
    return None if value is None else str(value)


#: The suffix of the annotation by which a class declares vocabulary instead
#: of using it. Matched across prefixes, and only with the colon, because an
#: unprefixed ``applies_to`` belongs to nobody.
_DECLARES_VOCABULARY = ":applies_to"


def is_model_object(obj: Any) -> bool:
    """Flag whether a class participates in the dimensional model.

    A class is part of the model because Varda says it is: one carrying only
    ``acme:`` annotations is a class an extension has decorated, not one it
    has enrolled, and extensions do not have the authority to enroll.

    A class declaring ``applies_to`` is excluded, whoever it belongs to. It
    is a profile's vocabulary declaration, and without this a model that
    imports a profile enrolls the profile's annotation classes as tables.
    """
    tags = annotations_of(obj)
    if any(tag.endswith(_DECLARES_VOCABULARY) for tag in tags):
        return False
    return any(tag.startswith("varda:") for tag in tags)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def schema(path: pathlib.Path) -> SchemaView:
    """Parse a profile or types schema, once per version of the file.

    Keyed on the file's size and modification time as well as its path, so
    the cache is never wrong about a file that changed and never needs
    clearing. Profiles are read on every registry built, and the suite
    builds hundreds.
    """
    stat = path.stat()
    return _parsed(str(path), stat.st_mtime_ns, stat.st_size)


@cache
def _parsed(path: str, _mtime: int, _size: int) -> SchemaView:
    return SchemaView(path)


# ---------------------------------------------------------------------------
# Reading a model
# ---------------------------------------------------------------------------


def read(path: str | pathlib.Path, registry: Registry) -> Model:
    """Read a domain model into a snapshot.

    ``registry`` supplies the import map, which is how a model writes
    ``imports: - varda`` without knowing where the installed profile lives,
    and is recorded on the model as the extension set it was read under.
    """
    source = pathlib.Path(path)
    view = SchemaView(str(source), importmap=dict(registry.importmap))
    schema_id = str(view.schema.id or "")
    files = _schema_files(view)

    tables = [
        _table(view, str(name), cls, schema_id, files)
        for name, cls in view.all_classes().items()
        if is_model_object(cls)
    ]
    tables.sort(key=lambda t: t.name)

    model = Model(
        source=source,
        schema_id=schema_id,
        tables=tuple(tables),
        declared_prefixes=frozenset(
            str(p) for p in (view.schema.prefixes or {})
        ),
        declared_types=frozenset(str(t) for t in view.all_types()),
        declared_classes=frozenset(str(c) for c in view.all_classes()),
        declared_enums=frozenset(str(e) for e in view.all_enums()),
        registry=registry,
    )
    _set(model, _by_name={t.name: t for t in tables})
    for table in tables:
        _set(table, model=model)
    # The second pass: what names another table can resolve only now.
    for table in tables:
        for column in table.columns:
            _resolve_references(model, table, column)
        _set(table, hierarchies=_hierarchies(model, table))
    return model


def _set(obj: object, **values: Any) -> None:
    """Set fields on a frozen element while it is still being built."""
    for name, value in values.items():
        object.__setattr__(obj, name, value)


def _schema_files(view: SchemaView) -> dict[str, str]:
    """Map every reachable schema's IRI to the file it was read from.

    The closure is walked explicitly because ``schema_map`` is empty until
    something has walked it.
    """
    view.imports_closure()
    out: dict[str, str] = {}
    for found in (view.schema_map or {}).values():
        source = getattr(found, "source_file", None)
        named = pathlib.Path(str(source)).name if source else found.name
        out[str(found.id)] = str(named)
    return out


def _table(
    view: SchemaView,
    name: str,
    cls: Any,
    schema_id: str,
    files: dict[str, str],
) -> Table:
    """Read one class as a table, everything but cross-table references."""
    declared_in = str(cls.from_schema or "")
    origin = (
        files.get(declared_in, declared_in)
        if declared_in and declared_in != schema_id
        else None
    )
    physical = text(cls, "varda:physical_name")
    table = Table(
        name=name,
        physical=physical or physical_name(name),
        physical_declared=bool(physical),
        role=_member(TableRole, text(cls, "varda:role")),
        fact_type=_member(FactType, text(cls, "varda:fact_type")),
        scd=_member(SlowlyChangingType, text(cls, "varda:scd")),
        grain=(),
        grain_statement=text(cls, "varda:grain_statement"),
        hierarchies=(),
        unique_keys=(),
        columns=(),
        description=(cls.description or "").strip(),
        origin=origin,
        instantiates=tuple(str(x) for x in (cls.instantiates or [])),
        annotations=Annotations(annotations_of(cls)),
    )
    columns = tuple(
        _column(view, str(s.name), s, table)
        for s in view.class_induced_slots(name)
    )
    by_name = {c.name: c for c in reversed(columns)}
    _set(table, columns=columns, _by_name=by_name)
    _set(
        table,
        grain=tuple(
            Ref(n, by_name.get(n))
            for n in _strings(annotations_of(cls).get("varda:grain"))
        ),
        unique_keys=_unique_keys(view, name, table),
    )
    return table


def _column(view: SchemaView, name: str, slot: Any, table: Table) -> Column:
    """Read one induced slot as a column."""
    rng = str(slot.range or "string")
    try:
        chain = tuple(str(t) for t in view.type_ancestors(rng)) or (rng,)
    except ValueError:
        chain = (rng,)  # V804 reports a range naming nothing
    physical = text(slot, "varda:physical_name")
    column = Column(
        name=name,
        physical=physical or physical_name(name),
        physical_declared=bool(physical),
        role=_member(ColumnRole, text(slot, "varda:role")),
        range=rng,
        type_chain=chain,
        required=bool(slot.required),
        references=None,
        additivity=_member(Additivity, text(slot, "varda:additivity")),
        semi_additive_over=None,
        unit=_unit(slot),
        max_length=None,
        precision=None,
        scale=None,
        description=(slot.description or "").strip(),
        instantiates=tuple(str(x) for x in (slot.instantiates or [])),
        annotations=Annotations(annotations_of(slot)),
    )
    _set(column, table=table, **{f: _facet(column, f) for f in FACETS})
    return column


def _resolve_references(model: Model, table: Table, column: Column) -> None:
    """Resolve the names a column gives for another table or column."""
    target = column.annotations.text("varda:references")
    if target is not None:
        name = target.strip()
        _set(column, references=Ref(name, model.table(name)))
    over = column.annotations.text("varda:semi_additive_over")
    if over is not None:
        name = over.strip()
        _set(column, semi_additive_over=Ref(name, table.column(name)))


def _member(enum: type[E], value: str | None) -> E | None:
    """Read a value as a member of a closed vocabulary, or ``None``.

    ``None`` for a value outside it as much as for an absent one. The
    annotation still carries what was written, and V002 reports it.
    """
    if value is None:
        return None
    try:
        return enum(value)
    except ValueError:
        return None


def _strings(value: Any) -> tuple[str, ...]:
    """Read an annotation as a tuple of strings.

    A YAML list arrives as a list and a bare scalar as a scalar, and both are
    legitimate ways to write a one-element annotation: `grain: order_id` is
    what someone writes for a single-column grain.
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (str(value),)
    if isinstance(value, Iterable):
        return tuple(str(v) for v in value)
    return (str(value),)


def _unit(slot: Any) -> str | None:
    """Read LinkML's ``unit`` as one label.

    One unit may be written as a symbol, a UCUM code, an abbreviation or a
    name, and a model may give any subset; they are tried in the order a
    reader would recognize them.
    """
    unit = getattr(slot, "unit", None)
    if unit is None:
        return None
    for name in ("symbol", "ucum_code", "abbreviation", "descriptive_name"):
        value = getattr(unit, name, None)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _facet(column: Column, name: str) -> int | None:
    """Read one type facet, or ``None`` if it cannot be emitted.

    ``None`` for an absent facet, one on a type it cannot parameterize, and
    one that is not a whole number at or above its floor. All three are
    V803's to report, and a number here for any of them would put a length
    on a date into the DDL generated under ``--force``.
    """
    if not column.parameterizes(name):
        return None
    raw = column.annotations.text(f"varda:{name}")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= FACET_MINIMUM[name] else None


def _unique_keys(
    view: SchemaView, name: str, table: Table
) -> tuple[UniqueKey, ...]:
    """Read every unique key that applies to a table, by name.

    Walked up the inheritance chain by hand. Columns arrive through
    ``class_induced_slots``, which inherits, but ``unique_keys`` does not, and
    a table that inherits its columns and silently loses the constraint over
    them is a disagreement nobody can debug. A name declared nearer wins.
    """
    seen: dict[str, UniqueKey] = {}
    for ancestor in view.class_ancestors(name):
        cls = view.get_class(ancestor)
        for key_name, key in (getattr(cls, "unique_keys", None) or {}).items():
            if str(key_name) in seen:
                continue  # the nearer declaration already won
            unique = UniqueKey(
                name=str(key_name),
                declared=tuple(
                    Ref(str(s), table.column(str(s)))
                    for s in key.unique_key_slots or ()
                ),
                description=(key.description or "").strip(),
            )
            _set(unique, table=table)
            seen[str(key_name)] = unique
    return tuple(seen[n] for n in sorted(seen))


def _hierarchies(model: Model, table: Table) -> tuple[Hierarchy, ...]:
    """Read the declared drill paths, in declared order.

    Entries that are not mappings are dropped rather than raising: a
    malformed hierarchy is V601's and V004's to report.
    """
    raw = table.annotations.get("varda:hierarchies")
    if raw is None:
        return ()
    entries = raw if isinstance(raw, list) else [raw]
    out: list[Hierarchy] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        specs = entry.get("levels")
        if isinstance(specs, (str, bytes, Mapping)):
            specs = [specs]
        hierarchy = Hierarchy(
            name=str(entry.get("name") or ""),
            description=str(entry.get("description") or "").strip(),
            levels=tuple(_level(model, table, v) for v in specs or ()),
        )
        _set(hierarchy, table=table)
        for level in hierarchy.levels:
            _set(level, hierarchy=hierarchy)
        out.append(hierarchy)
    return tuple(out)


def _level(model: Model, table: Table, entry: Any) -> Level:
    """Resolve one level, plain or reached through a foreign key.

    A level is written as a bare column name wherever that column both names
    and identifies it. The mapping form — ``{column: product_name, key:
    sku}`` — is for a level whose name is not what tells its members apart.
    """
    if isinstance(entry, Mapping):
        spec = str(entry.get("column") or "")
        declared_key = str(entry.get("key") or "")
    else:
        spec, declared_key = str(entry), ""
    head, dot, tail = spec.partition(".")
    near = table.column(head)
    column = near
    via = None
    owner = table
    if dot:
        via = near
        column = None
        if near is not None and near.references is not None:
            found = model.table(near.references.name)
            owner = found or table
            column = found.column(tail) if found is not None else None
    # A declared key is looked up wherever the naming column came from:
    # `column` and `key` describe one level, so a level reached through a
    # foreign key is named and identified by the same dimension.
    key = (
        Ref(declared_key, owner.column(declared_key)) if declared_key else None
    )
    return Level(spec=spec, via=via, column=column, key=key)
