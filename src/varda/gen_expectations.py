"""Expectation generation — what the model says about its data, as queries.

`assertions.sql` holds the claims a constraint could have enforced, and
returns nothing exactly when the dropped constraints would have held. This
file holds the claims no constraint can state: that a type-2 dimension's
history is sound, and that a hierarchy rolls up the way it says. They are
the model's claims and not Varda's — each follows from roles and
hierarchies the model already declares — and they live in their own file
so that `assertions.sql` keeps the one meaning it has.

Each query returns the rows breaking its claim, and nothing when it holds.
Written in the same plain SQL as the assertions, for the same reason: a
warehouse that cannot enforce a key is exactly the one that most needs the
check. A flag is read as `CAST(flag AS INTEGER) = 1`, the one spelling of
"is true" that a `BIT` and a `BOOLEAN` share.

**Only what the model claims.** A version period is closed at its start and
open at its end, as the profile says, so a period ending where it starts is
in force for no instant at all, and two versions overlap when one starts
before the other ends. The current row is checked only where a flag says
which one it is: a window whose open end is a far-future sentinel has no
current row this file could find without guessing the sentinel, and the
overlap check already catches two open versions.

**A level is checked where its key identifies on its own.** A plain level's
column identifies a member only under its parent — several states have a
Springfield — so "brand Acme under two categories" is two members and not a
fault, and nothing here says otherwise. A level reached through a foreign
key is different: the key names one row of another dimension, so it can sit
under one parent and no other. And where a level declares a key apart from
its name, every row sharing that key must share the name.

**Not for a type-2 dimension's hierarchies.** Its rows are versions, and a
city moving region is history rather than a contradiction. Checking each
version on its own would need the current row, which only some designs
mark; the hierarchy checks wait for a declaration of that.

Nulls follow SQL's reading, as in the assertions: a row whose key is null
is not part of any claim about that key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import analysis
from .gen_assertions import LABEL
from .gen_sql import quoted
from .snapshot import VERSIONING_ROLES, SlowlyChangingType

if TYPE_CHECKING:
    from .ext import Context
    from .snapshot import Column, Hierarchy, Level, Model, Table

#: The artifact this generator writes.
ARTIFACT = "sql/expectations.sql"

#: What the file says for a model claiming nothing it can check. An empty
#: file reads as a generator that failed quietly.
NOTHING_CLAIMED = (
    "-- This model states no claim about its data that a constraint cannot."
)


def _names(columns: tuple[Column, ...], alias: str = "") -> str:
    """Render columns as a select list, qualified by ``alias`` if given."""
    prefix = f"{quoted(alias)}." if alias else ""
    return ", ".join(f"{prefix}{quoted(c.physical)}" for c in columns)


def _shown(columns: tuple[Column, ...]) -> str:
    """Render columns the way a label names them."""
    return f"({', '.join(quoted(c.physical) for c in columns)})"


def _present(columns: tuple[Column, ...], alias: str = "") -> list[str]:
    """Render the predicate keeping rows that hold every column."""
    prefix = f"{quoted(alias)}." if alias else ""
    return [
        f"{'WHERE' if i == 0 else '  AND'} {prefix}{quoted(c.physical)} "
        "IS NOT NULL"
        for i, c in enumerate(columns)
    ]


def _relation(table: Table, schema: str, alias: str = "") -> str:
    """Render a table reference, aliased if ``alias`` is given."""
    name = f"{quoted(schema)}.{quoted(table.physical)}"
    return f"{name} AS {quoted(alias)}" if alias else name


def _flagged(column: Column, alias: str = "") -> str:
    """Render "this flag is true" in the spelling every engine reads."""
    prefix = f"{quoted(alias)}." if alias else ""
    return f"CAST({prefix}{quoted(column.physical)} AS INTEGER) = 1"


def _same(columns: tuple[Column, ...], left: str, right: str) -> list[str]:
    """Render the join of two aliases on equal values of ``columns``."""
    return [
        f"{quoted(right)}.{quoted(c.physical)} = "
        f"{quoted(left)}.{quoted(c.physical)}"
        for c in columns
    ]


# ---------------------------------------------------------------------------
# Type 2 — the history is sound
# ---------------------------------------------------------------------------


def business_keys(table: Table) -> tuple[tuple[Column, ...], ...]:
    """Name what identifies one entity across its versions, each way it is.

    Read from the table's own unique claims: a type-2 dimension is unique
    on a business key plus something marking versions apart, so each claim
    holding a versioning column, less that column, is one business key. Two
    sources identifying a product two ways give two keys, and each is
    checked on its own. A claim with nothing left gives none.
    """
    out: list[tuple[Column, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for claim in analysis.unique_claims(table):
        if not any(c.role in VERSIONING_ROLES for c in claim):
            continue
        key = tuple(c for c in claim if c.role not in VERSIONING_ROLES)
        names = tuple(c.physical for c in key)
        if key and names not in seen:
            seen.add(names)
            out.append(key)
    return tuple(out)


def _empty_periods(table: Table, schema: str) -> str:
    """Render the versions whose period holds no instant."""
    start, end = table.version_starts[0], table.version_ends[0]
    return "\n".join(
        [
            (
                f"{LABEL}{table.physical}: every version is in force for a "
                f"while ({quoted(start.physical)} < {quoted(end.physical)})"
            ),
            'SELECT count(*) AS "empty_versions"',
            f"FROM {_relation(table, schema)}",
            f"WHERE {quoted(end.physical)} <= {quoted(start.physical)};",
        ]
    )


def _overlaps(table: Table, schema: str, key: tuple[Column, ...]) -> str:
    """Render the versions of one entity that are in force at once.

    `b` starts after `a` and before `a` ends, or `a` never ends. Strictly
    after: two versions starting together break the table's unique key,
    which `assertions.sql` already reports.
    """
    start, end = table.version_starts[0], table.version_ends[0]
    s, e = quoted(start.physical), quoted(end.physical)
    join = _same(key, "a", "b")
    return "\n".join(
        [
            (
                f"{LABEL}{table.physical}: versions of {_shown(key)} do "
                "not overlap"
            ),
            f'SELECT {_names(key, "a")}, count(*) AS "overlaps"',
            f"FROM {_relation(table, schema, 'a')}",
            f"JOIN {_relation(table, schema, 'b')}",
            f"  ON {join[0]}",
            *(f" AND {line}" for line in join[1:]),
            f' AND "b".{s} > "a".{s}',
            f' AND ("a".{e} IS NULL OR "a".{e} > "b".{s})',
            f"GROUP BY {_names(key, 'a')};",
        ]
    )


def _one_current(table: Table, schema: str, key: tuple[Column, ...]) -> str:
    """Render the entities more than one of whose versions is current."""
    flag = table.current_flags[0]
    names = _names(key)
    return "\n".join(
        [
            (
                f"{LABEL}{table.physical}: at most one version of "
                f"{_shown(key)} is current"
            ),
            f'SELECT {names}, count(*) AS "current_versions"',
            f"FROM {_relation(table, schema)}",
            f"WHERE {_flagged(flag)}",
            *(line.replace("WHERE", "  AND", 1) for line in _present(key)),
            f"GROUP BY {names}",
            "HAVING count(*) > 1;",
        ]
    )


def _latest_current(
    table: Table, schema: str, key: tuple[Column, ...], mark: Column
) -> str:
    """Render the current versions that a later version supersedes."""
    flag = table.current_flags[0]
    m = quoted(mark.physical)
    join = _same(key, "cur", "later")
    return "\n".join(
        [
            (
                f"{LABEL}{table.physical}: the current version of "
                f"{_shown(key)} is its latest"
            ),
            'SELECT count(*) AS "superseded_current"',
            f"FROM {_relation(table, schema, 'cur')}",
            f"WHERE {_flagged(flag, 'cur')}",
            "  AND EXISTS (",
            "    SELECT 1",
            f"    FROM {_relation(table, schema, 'later')}",
            f"    WHERE {join[0]}",
            *(f"      AND {line}" for line in join[1:]),
            f'      AND "later".{m} > "cur".{m}',
            "  );",
        ]
    )


def _history(table: Table, schema: str) -> list[str]:
    """Every claim a type-2 dimension's versioning columns make."""
    if table.scd != SlowlyChangingType.TYPE_2:
        return []
    window = bool(table.version_starts and table.version_ends)
    out = [_empty_periods(table, schema)] if window else []
    marks = table.version_starts or table.version_numbers
    for key in business_keys(table):
        if window:
            out.append(_overlaps(table, schema, key))
        if table.current_flags:
            out.append(_one_current(table, schema, key))
            if marks:
                out.append(_latest_current(table, schema, key, marks[0]))
    return out


# ---------------------------------------------------------------------------
# Hierarchies — each member rolls up into one parent
# ---------------------------------------------------------------------------


def _functional(
    relation: str,
    label: str,
    determinant: tuple[Column, ...],
    dependent: tuple[Column, ...],
    count_as: str,
) -> str:
    """Render the values of ``determinant`` seen with several ``dependent``.

    Counted over distinct pairs rather than with `count(DISTINCT ...)`,
    which takes one column in most engines and a tuple in few.
    """
    both = (*determinant, *dependent)
    names = _names(determinant)
    return "\n".join(
        [
            f"{LABEL}{label}",
            f'SELECT {names}, count(*) AS "{count_as}"',
            "FROM (",
            f"  SELECT DISTINCT {_names(both)}",
            f"  FROM {relation}",
            *(f"  {line}" for line in _present(both)),
            ') AS "pairs"',
            f"GROUP BY {names}",
            "HAVING count(*) > 1;",
        ]
    )


def _own(table: Table, columns: tuple[Column, ...]) -> bool:
    """Flag whether every column is one of ``table``'s own."""
    return bool(columns) and all(c.table is table for c in columns)


def _roll_up(
    table: Table, schema: str, hierarchy: Hierarchy, i: int
) -> str | None:
    """Render one level reached through a foreign key rolling up, or nothing.

    The foreign key names one row of another dimension, so it is a whole
    identity on its own and can sit under one parent only. A level that
    declares its own key is identified in the table it reaches, which this
    table's rows cannot see, and is left alone.
    """
    level: Level = hierarchy.levels[i]
    via = level.via
    if not level.is_reference or level.key is not None or via is None:
        return None
    if analysis.reference(via) is None:
        return None  # V402, V403 or V406 says why
    parent = analysis.identities(hierarchy)[i - 1]
    if not _own(table, parent):
        return None
    above = hierarchy.levels[i - 1]
    label = (
        f"{table.physical}.{hierarchy.name}: each {level.spec} is in one "
        f"{above.spec}"
    )
    relation = _relation(table, schema)
    return _functional(relation, label, (via,), parent, "parents")


def _named_once(
    table: Table, schema: str, hierarchy: Hierarchy, i: int
) -> str | None:
    """Render one level whose key is not its name, named one way, or nothing.

    The key says which member a row belongs to, and the column what that
    member is called; one member with two names is two members that the key
    cannot tell apart.
    """
    level: Level = hierarchy.levels[i]
    column = level.column
    key = analysis.level_key(level)
    if level.is_reference or level.key is None or column is None:
        return None
    if key is None or key is column:
        return None
    identity = analysis.identities(hierarchy)[i]
    if not _own(table, (*identity, column)):
        return None
    label = (
        f"{table.physical}.{hierarchy.name}: each {_shown(identity)} is "
        f"named one {quoted(column.physical)}"
    )
    relation = _relation(table, schema)
    return _functional(relation, label, identity, (column,), "names")


def _hierarchies(table: Table, schema: str) -> list[str]:
    """Every roll-up claim a table's hierarchies make that rows can show."""
    if table.scd == SlowlyChangingType.TYPE_2:
        return []
    out: list[str] = []
    for hierarchy in table.hierarchies:
        for i in range(len(hierarchy.levels)):
            if i > 0:
                found = _roll_up(table, schema, hierarchy, i)
                if found is not None:
                    out.append(found)
            named = _named_once(table, schema, hierarchy, i)
            if named is not None:
                out.append(named)
    return out


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def generate(model: Model, schema: str = "mart") -> str:
    """Render every expectation in the model as one runnable script."""
    header = [
        "-- Generated by varda. Do not edit.",
        f"-- Source: {model.source.name}",
        "--",
        "-- Every claim the model states about its data that no constraint",
        "-- can, as a query returning the rows that break it. Each one",
        "-- returns nothing when the claim holds.",
        "--",
        "-- Version periods are closed at the start and open at the end. A",
        "-- row whose key is null is not part of any claim about that key.",
        "",
        "",
    ]
    blocks = [
        block
        for table in analysis.creation_order(model)
        for block in (*_history(table, schema), *_hierarchies(table, schema))
    ]
    if not blocks:
        blocks = [NOTHING_CLAIMED]
    return "\n".join(header) + "\n\n".join(blocks) + "\n"


def run(ctx: Context) -> dict[str, str]:
    """Run this generator and return its artifacts."""
    return {ARTIFACT: generate(ctx.model, ctx.options.schema)}
