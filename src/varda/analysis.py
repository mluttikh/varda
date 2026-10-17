"""What Varda concludes from a model.

:mod:`varda.snapshot` records what a model says. This module is what Varda
reads into it — the uniqueness a dimension implies by its roles, the key a
foreign key lands on, the order tables are created in, what identifies a
level of a drill path. Each answer is computed in one place because more
than one generator, or a generator and a rule, needs it, and one claim
computed twice is one claim that eventually disagrees with itself.

The promise is weaker than the snapshot's and is stated as such: a minor
release may make an answer here stricter — a derived key that declines to
guess in one more case — so long as the signature holds. An extension that
needs the model's literal statement reads the snapshot instead.

Every function is total over a model that fails `check`. A generator never
reads :attr:`varda.snapshot.Ref.target` itself; it asks here, and what a
half-broken model looks like under ``--force`` is decided once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .snapshot import SlowlyChangingType

if TYPE_CHECKING:
    from .snapshot import Column, Hierarchy, Level, Model, Table

_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: Above this many natural keys, what a dimension is unique on stops being
#: derivable from the roles alone: several may be one compound identity or
#: several alternative ones, and the two want opposite constraints. Named
#: because :func:`unique_claims` and V306 have to agree about it.
ONE_IDENTITY = 1


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
class Reference:
    """Where one foreign key lands: a table, and the key it joins to."""

    table: Table
    key: Column


def reference(column: Column) -> Reference | None:
    """Resolve a foreign key to the table and key it lands on.

    ``None`` when there is nothing to land on — no target named, a target
    that is not a table, or one with no surrogate key. Each is a rule's
    finding (V402, V403, V301), and a generator emits no reference for it
    rather than inventing one.
    """
    ref = column.references
    target = ref.target if ref is not None else None
    if target is None or not target.surrogate_keys:
        return None
    return Reference(table=target, key=target.surrogate_keys[0])


def type_params(column: Column) -> tuple[int, ...]:
    """Give the parameters a column's type is emitted with, or none.

    ``(80,)`` for a ``VARCHAR(80)``, ``(18, 2)`` for a ``NUMERIC(18, 2)``.
    Empty for a column that declares none: Varda emits the bare type rather
    than inventing a default width, because a default is a number nobody
    chose, wrong in the direction that looks like working output. V707 asks
    for it where it costs most.

    A scale that cannot be emitted is dropped rather than guessed at — one
    with no precision beside it, since there is no `NUMERIC(, 2)`, and one
    larger than its precision, which no database accepts. V803 reports both;
    this is what ``--force`` writes in the meantime.
    """
    if column.max_length is not None:
        return (column.max_length,)
    precision = column.precision
    if precision is None:
        return ()
    scale = column.scale
    if scale is None or scale > precision:
        return (precision,)
    return (precision, scale)


#: What a value of each range is stored as, where two ranges share one.
#:
#: The string family is one storage, because every generator renders all
#: four the same way — `VARCHAR`, `sa.String` — and a `uri` key referenced
#: by a `string` column joins character for character. `float` and `double`
#: are one storage for the same reason. Everything else is its own, and
#: `uuid` above all: it is declared `typeof: string`, and a `UUID` key is
#: still not something a `VARCHAR` column can reference.
#:
#: The keys are :data:`varda.gen_sql.TYPES`'s and a test says so, and a
#: test holds every dialect to rendering one storage as one type.
STORAGE: dict[str, str] = {
    "string": "string",
    "uri": "string",
    "uriorcurie": "string",
    "ncname": "string",
    "integer": "integer",
    "float": "double",
    "double": "double",
    "decimal": "decimal",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "time": "time",
    "uuid": "uuid",
}


def storage(column: Column) -> str | None:
    """Name what a column's values are stored as, or ``None`` if unknown.

    Resolved through the type chain nearest first, as the generators resolve
    it, so a schema's own ``key: {typeof: integer}`` stores an integer and
    ``uuid`` stores a UUID rather than the string it is declared in terms of.
    ``None`` for a range naming nothing, which is V804's to report, and for
    a real type no generator stores, which is that generator's.
    """
    return next(
        (STORAGE[t] for t in column.type_chain if t in STORAGE),
        None,
    )


def level_key(level: Level) -> Column | None:
    """Name what tells one member of a level from another, under one parent.

    The key the model declared, where it declared one. Otherwise the foreign
    key for a level reached through one, and the naming column for a plain
    level — the common case, a level whose name is what tells its members
    apart.
    """
    if level.key is not None:
        return level.key.target
    return level.via if level.is_reference else level.column


def identities(hierarchy: Hierarchy) -> tuple[tuple[Column, ...], ...]:
    """Name what tells one member of each level from every other, in order.

    A level's key preceded by the key of every coarser level. `city_name`
    holds "Springfield" for cities in three states, and country, state and
    city together hold one of them. That path is what a hierarchy already
    asserts, so it is derived rather than written down. A level whose key
    resolves to nothing adds nothing to the path.
    """
    out: list[tuple[Column, ...]] = []
    path: tuple[Column, ...] = ()
    for level in hierarchy.levels:
        key = level_key(level)
        if key is not None:
            path = (*path, key)
        out.append(path)
    return tuple(out)


def unique_claims(table: Table) -> tuple[tuple[Column, ...], ...]:
    """Every distinct combination this table claims is unique.

    The primary key is not among them. A surrogate key is the table's
    identity rather than a claim made about it; everything here is a claim
    the model states on top of that, and every one is optional in the DDL.

    The grain leads, because it is the table's own statement of what a row
    is. Then LinkML's own ``unique_keys``, one claim each: a table identified
    two ways by two sources needs two claims, and merging them produces one
    weaker than either. Then, for a dimension that declares no keys, the
    uniqueness its natural key implies — V302 requires that key and calls it
    what a loader matches on, and leaving it unstated makes it a claim
    nobody checks.

    Each combination once. A grain and a declared key may name the same
    columns, and every database accepts the doubled constraint by building a
    second index for it.
    """
    claims: list[tuple[Column, ...]] = []
    if table.grain_columns:
        claims.append(table.grain_columns)
    # A key naming nothing is V303's to report, and claims nothing here.
    claims.extend(u.columns for u in table.unique_keys if u.columns)
    if table.is_dimension and not table.unique_keys:
        derived = _derived_key(table)
        if derived:
            claims.append(derived)

    out: list[tuple[Column, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for columns in claims:
        names = tuple(c.physical for c in columns)
        if names in seen:
            continue
        seen.add(names)
        out.append(columns)
    return tuple(out)


def _derived_key(table: Table) -> tuple[Column, ...]:
    """Derive what a dimension is unique on from its roles alone.

    Empty whenever the answer cannot be reached without guessing, and each
    way that happens is reported elsewhere. Silence is the safe direction:
    claiming a natural key alone on a table that turns out to version would
    reject the second version of every row.

    Two natural keys are one of those ways. They mean either one compound
    identity or two alternative ones, and the two want opposite claims;
    deriving the merged form for a table that meant the second is worse than
    deriving nothing. V306 asks the model to say which.
    """
    if not table.natural_keys:
        return ()  # V302 reports it
    if len(table.natural_keys) > ONE_IDENTITY:
        return ()  # V306 reports it
    if table.scd in {SlowlyChangingType.TYPE_0, SlowlyChangingType.TYPE_1}:
        # Neither keeps a second row for one business entity, so the
        # natural key is the whole of it.
        return table.natural_keys
    if table.scd == SlowlyChangingType.TYPE_2:
        # The natural key repeats once per version, so the uniqueness is
        # the natural key plus one column marking versions apart. One, not
        # both: `(nk, start, number)` permits two rows sharing a natural key
        # and a start that differ only in their counter, which either alone
        # forbids. The start wins, being the more specific claim, and
        # `is_current` is no discriminator at all — it is true of one row.
        marks = table.version_starts or table.version_numbers
        return (*table.natural_keys, marks[0]) if marks else ()  # V506
    return ()  # no varda:scd — V502 reports it, and guessing costs rows


def creation_order(model: Model) -> tuple[Table, ...]:
    """Order the tables so every reference target precedes its referrer.

    Dimensions and bridges are ordered together, because either may
    reference the other. Facts come last and need no ordering among
    themselves, since nothing may reference a fact. Ties are broken by name,
    so a model with no table-to-table references emits in name order.

    Total: a set of tables that reference each other in a cycle is emitted
    by name, and :func:`reference_cycle` names them. Whether that is an
    error depends on whether the output declares the references, which is a
    generator's question.
    """
    ordered, _ = _by_dependency((*model.dimensions, *model.bridges))
    return (*ordered, *model.facts)


def reference_cycle(model: Model) -> tuple[str, ...]:
    """Name the tables whose references form a cycle, or none.

    A self-reference is not a cycle. `FOREIGN KEY (manager_key) REFERENCES
    dim_employee` inside `CREATE TABLE dim_employee` is legal SQL, and a
    recursive dimension is common enough that refusing it would refuse a
    normal model.
    """
    _, cycle = _by_dependency((*model.dimensions, *model.bridges))
    return cycle


def _by_dependency(
    tables: tuple[Table, ...],
) -> tuple[tuple[Table, ...], tuple[str, ...]]:
    """Order tables by reference, returning any that could not be placed."""
    remaining = {t.name: t for t in tables}
    out: list[Table] = []
    stuck: tuple[str, ...] = ()
    while remaining:
        ready = sorted(
            name
            for name, table in remaining.items()
            if not _pending(table, remaining)
        )
        if not ready:
            stuck = stuck or tuple(sorted(remaining))
            ready = sorted(remaining)
        out += [remaining.pop(name) for name in ready]
    return tuple(out), stuck


def _pending(table: Table, remaining: dict[str, Table]) -> set[str]:
    """Name the tables this one references that are not yet placed."""
    targets = {
        fk.references.target.name
        for fk in table.foreign_keys
        if fk.references is not None and fk.references.target is not None
    }
    return (targets & set(remaining)) - {table.name}
