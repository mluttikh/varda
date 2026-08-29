"""Assertion generation — the model's claims as queries over the data.

Every claim the DDL can be asked not to enforce, written as a query that
returns the rows breaking it and nothing when it holds. This is the other
half of ``--constraints``: turning enforcement off moves a claim out of the
engine, and this is where it moves to.

Three reasons it is worth having even where enforcement is on.

It runs **once per load rather than once per row**. A constraint is checked
on every write for the life of the table; an assertion is checked when the
data changes, which is the moment the answer can differ.

It **reports**. A violated constraint aborts a transaction and names one
row. A violated assertion hands back every offending key and how many rows
carry it, which is what somebody fixing a loader needs.

It is **plain SQL**. `SELECT`, `LEFT JOIN`, `GROUP BY ... HAVING` — no
dialect owns any of it, so a warehouse that cannot enforce a key at all
still gets the check. That is the whole gap: on a lakehouse this file is not
a cheaper alternative to the constraints, it is the only form of them there
is.

Written the long way on purpose. Positional `GROUP BY 1, 2` and
`JOIN ... USING` are both shorter and neither exists in T-SQL, and the
portability is the entire argument.

Nulls follow SQL's reading, which is the one the constraints this replaces
use: a `UNIQUE` key holding a null is not a duplicate, and a null foreign
key is not an orphan. An assertion stricter than the constraint it stands in
for would report rows the database would have accepted, and a check that
cries wolf is one people switch off.

Nothing here asserts `NOT NULL` on an ordinary column or the width of a
type. Those are never dropped at any level, so there is nothing to relocate
— and asserting them anyway would be this file starting to make claims of
its own, which is the line it must not cross.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .gen_sql import quoted

if TYPE_CHECKING:
    from .ext import Context
    from .model import Column, DimensionalModel, Table

#: Prefix for a claim's comment, so a violation found by running the file
#: can be traced back to the model that stated it.
LABEL = "-- "


def _not_null(columns: tuple[Column, ...]) -> list[str]:
    """Render the predicate excluding rows SQL would never call duplicates.

    One line per column rather than one long conjunction. A compound grain
    runs past the width a terminal shows at three columns, and generated
    output is read by people even though it is written by a machine.
    """
    return [
        f"{'WHERE' if i == 0 else '  AND'} {quoted(c.physical)} IS NOT NULL"
        for i, c in enumerate(columns)
    ]


def _duplicates(
    table: Table, schema: str, columns: tuple[Column, ...], label: str
) -> str:
    """Render one uniqueness claim as a query for the rows that break it."""
    names = ", ".join(quoted(c.physical) for c in columns)
    return "\n".join(
        [
            f"{LABEL}{table.physical}: {label} ({names})",
            f'SELECT {names}, count(*) AS "row_count"',
            f"FROM {quoted(schema)}.{quoted(table.physical)}",
            *_not_null(columns),
            f"GROUP BY {names}",
            "HAVING count(*) > 1;",
        ]
    )


def _populated(table: Table, schema: str, column: Column) -> str:
    """Render the half of a primary key that is not about uniqueness."""
    return "\n".join(
        [
            f"{LABEL}{table.physical}: {column.physical} is populated",
            'SELECT count(*) AS "nulls"',
            f"FROM {quoted(schema)}.{quoted(table.physical)}",
            f"WHERE {quoted(column.physical)} IS NULL;",
        ]
    )


def _orphans(fk: Column, schema: str, target: Table, key: Column) -> str:
    """Render one foreign key as a query for the rows with no target row.

    Aliased `src` and `tgt` rather than by initial, so a dimension that
    references itself — a reporting line, a parent product — joins to two
    distinct names instead of to one ambiguous one.
    """
    source = fk.table
    return "\n".join(
        [
            (
                f"{LABEL}{source.physical}.{fk.physical} -> "
                f"{target.physical}.{key.physical}"
            ),
            'SELECT count(*) AS "orphans"',
            f'FROM {quoted(schema)}.{quoted(source.physical)} AS "src"',
            f'LEFT JOIN {quoted(schema)}.{quoted(target.physical)} AS "tgt"',
            (
                f'       ON "src".{quoted(fk.physical)} '
                f'= "tgt".{quoted(key.physical)}'
            ),
            f'WHERE "src".{quoted(fk.physical)} IS NOT NULL',
            f'  AND "tgt".{quoted(key.physical)} IS NULL;',
        ]
    )


def _for_table(table: Table, schema: str) -> list[str]:
    """Every assertion one table's own declarations produce, in model order.

    The primary key first, because it is the table's identity and the only
    claim `--constraints none` drops without leaving a comment behind. Then
    what the model says is unique, then what it says it references — the
    order the DDL states them in, so the two files can be read side by side.
    """
    out: list[str] = []
    for key in table.surrogate_keys:
        out.append(_duplicates(table, schema, (key,), "PRIMARY KEY"))
        out.append(_populated(table, schema, key))
    out += [
        _duplicates(table, schema, columns, "UNIQUE")
        for columns in table.unique_claims
    ]
    for fk in table.foreign_keys:
        target = table.model.table(fk.references or "")
        if target is None:
            continue  # V403 already reported it; do not also crash here
        if not target.surrogate_keys:
            continue  # V301's business
        out.append(_orphans(fk, schema, target, target.surrogate_keys[0]))
    return out


def generate(model: DimensionalModel, schema: str = "mart") -> str:
    """Render every claim in the model as one runnable script."""
    header = [
        "-- Generated by varda. Do not edit.",
        f"-- Source: {model.source.name}",
        "--",
        "-- Every claim the model states that a database can be asked not",
        "-- to enforce, as a query returning the rows that break it. Each",
        "-- one returns nothing when the claim holds.",
        "--",
        "-- Run after a load, not on every write. Nulls follow SQL's",
        "-- reading: a key holding a null is not a duplicate, and a null",
        "-- foreign key is not an orphan.",
        "",
        "",
    ]
    blocks = [
        block
        for table in (*model.dimensions, *model.bridges, *model.facts)
        for block in _for_table(table, schema)
    ]
    if not blocks:
        # A model with no keys and no references states nothing this file
        # can carry. Saying so beats an empty file, which reads as a
        # generator that failed quietly.
        blocks = ["-- This model states no claim that a database enforces."]
    return "\n".join(header) + "\n\n".join(blocks) + "\n"


def run(ctx: Context) -> dict[str, str]:
    """Run this generator and return its artifacts."""
    return {"sql/assertions.sql": generate(ctx.model, ctx.schema)}
