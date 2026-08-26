"""SQL DDL generation.

Emits ``CREATE TABLE`` for every table in the model, in an order that lets the
whole file run against an empty database: every table follows the ones it
references, because a foreign key cannot be declared before its target exists.
Facts fall last on their own, since nothing may reference a fact.

Nothing here is timestamped and nothing depends on the environment. That is
not a stylistic preference — a generated file that changes when nothing about
the model changed makes "is this output current?" unanswerable, and turns
every regeneration into a diff nobody reads.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ext import Context
    from .model import Column, DimensionalModel, Table

#: LinkML range to SQL type. Deliberately a closed table with no fallback:
#: an unmapped range raises rather than quietly becoming TEXT, because a
#: column silently typed as text is a bug that surfaces years later as a
#: comparison that does not do what it looks like.
TYPES = {
    "string": "VARCHAR",
    "integer": "INTEGER",
    "float": "DOUBLE PRECISION",
    "double": "DOUBLE PRECISION",
    "decimal": "NUMERIC",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "time": "TIME",
    "uri": "VARCHAR",
    "uriorcurie": "VARCHAR",
    "ncname": "VARCHAR",
}


class GenerationError(Exception):
    """A model cannot be generated from, and the run must stop."""


def sql_type(column: Column) -> str:
    """Map a column's range to a SQL type, or raise naming the column."""
    rng = column.range
    mapped = TYPES.get(rng.lower())
    if mapped is None:
        known = ", ".join(sorted(TYPES))
        msg = (
            f"{column}: range {rng!r} has no SQL mapping. Known ranges: {known}"
        )
        raise GenerationError(msg)
    return mapped + _facets(column)


def _facets(column: Column) -> str:
    """Render the type parameters a column declares, or nothing.

    Nothing is the honest output for a column that declares none, but it is
    not a safe one everywhere, and that is the whole reason the facets exist.
    A bare `VARCHAR` is unbounded in PostgreSQL and DuckDB and means
    `VARCHAR(1)` in SQL Server, so every string column in a generated star
    truncates to one character on one major engine. A bare `NUMERIC` is
    exact and unconstrained in PostgreSQL and `DECIMAL(18, 3)` in DuckDB,
    where a unit price of 0.123456 is stored as 0.123 and nothing says so.

    Varda emits the bare type anyway when nothing is declared, rather than
    inventing a default width. A default is the same silent fallback
    :data:`TYPES` refuses when it raises on an unmapped range: it would put a
    number nobody chose into the DDL, and be wrong in the direction that
    looks like working output. `V707` asks the question where it costs most.

    A scale that cannot be emitted is dropped rather than guessed at, and
    there are two of those: one with no precision beside it, since there is
    no `NUMERIC(, 2)` and the missing half would have to be invented, and one
    larger than its precision, which no database accepts. V803 reports both
    as errors; this is what `--force` writes in the meantime.
    """
    if column.max_length is not None:
        return f"({column.max_length})"
    precision = column.precision
    if precision is None:
        return ""
    scale = column.scale
    if scale is None or scale > precision:
        return f"({precision})"
    return f"({precision}, {scale})"


def quoted(name: str) -> str:
    """Render one identifier so SQL reads it as a name and nothing else.

    Every identifier is quoted, never only the ones that look dangerous.
    A reserved-word list is a silent fallback in the same way an unmapped
    range defaulting to TEXT would be: a word missing from it emits DDL that
    fails to parse, and the list has to track every engine the output might
    run on. `select` as a column name passed `varda check` with no findings
    and generated a file no database would accept.

    Quoting also preserves a physical name's case. An unquoted identifier
    folds to lower case in PostgreSQL, so a model declaring
    `varda:physical_name: DimOrder` got a table called `dimorder`; quoted, it
    gets the name it asked for. Derived names are already lower snake case,
    so nothing else moves.

    An embedded quote is doubled, following SQL:2016.
    """
    return '"' + name.replace('"', '""') + '"'


def _comment(text: str, label: str = "") -> list[str]:
    """Wrap a comment at the line limit.

    The output is read by people even though it is written by a machine, and
    a comment that runs to 300 columns is one nobody reads in a terminal.
    """
    body = f"{label}{text}" if label else text
    return [f"-- {line}" for line in textwrap.wrap(body, width=77)]


def _pending(table: Table, remaining: dict[str, Table]) -> set[str]:
    """Name the tables this one references that are not yet created.

    A self-reference is not pending. `FOREIGN KEY (manager_key) REFERENCES
    dim_employee` inside `CREATE TABLE dim_employee` is legal SQL, and a
    recursive dimension is common enough that treating it as a cycle would
    refuse a normal model.
    """
    targets = {(c.references or "") for c in table.foreign_keys}
    return (targets & set(remaining)) - {table.name}


def _by_dependency(tables: tuple[Table, ...]) -> list[Table]:
    """Order tables so every reference target precedes the table using it.

    Sorting by name is not enough once one dimension references another,
    which is what a snowflake is: `DimCity` sorts before `DimState` and
    references it, so the emitted file creates a table whose target does not
    exist yet and stops there.

    Ties are broken by name, so a model with no dimension-to-dimension
    references emits in exactly the order it did before — a flat star sees no
    diff.
    """
    remaining = {t.name: t for t in tables}
    out: list[Table] = []
    while remaining:
        ready = sorted(
            name
            for name, table in remaining.items()
            if not _pending(table, remaining)
        )
        if not ready:
            # Every remaining table waits on another one. With the foreign
            # keys inline in CREATE TABLE there is no order that works, so
            # this raises rather than emitting a file that cannot run —
            # the same treatment an unmapped range gets. Breaking the cycle
            # needs ALTER TABLE after creation, which is a feature and not
            # a fallback.
            names = ", ".join(sorted(remaining))
            msg = (
                f"foreign keys form a cycle among: {names}. "
                f"No CREATE TABLE order satisfies them."
            )
            raise GenerationError(msg)
        out += [remaining.pop(name) for name in ready]
    return out


def _ordered(model: DimensionalModel) -> list[Table]:
    """Order tables so every foreign key's target is already created.

    Dimensions and bridges are ordered together rather than as two blocks.
    A bridge references dimensions, but a dimension may reference a bridge
    too — V404 permits both — and two fixed blocks cannot express that.
    Facts come last and need no ordering among themselves, because nothing
    may reference a fact.
    """
    return [
        *_by_dependency((*model.dimensions, *model.bridges)),
        *model.facts,
    ]


def _column_line(column: Column) -> str:
    parts = [f"    {quoted(column.physical)}", sql_type(column)]
    if column.role == "SURROGATE_KEY":
        parts.append("PRIMARY KEY")
    elif column.required:
        parts.append("NOT NULL")
    return " ".join(parts)


def _unique_constraints(table: Table) -> list[str]:
    """Render the uniqueness constraints that apply to one table."""
    out: list[str] = []
    # The grain, as a constraint the database enforces rather than a comment
    # it ignores. This is the whole return on declaring `varda:grain` as
    # columns: a uniqueness claim nobody checks is a uniqueness claim that
    # stops being true, quietly, on some load nobody is watching.
    grain = table.grain_columns
    if grain:
        cols = ", ".join(quoted(c.physical) for c in grain)
        out.append(f"    UNIQUE ({cols})")

    # LinkML's own `unique_keys`, when a model declares them, and nothing
    # derived. One combination per constraint rather than every key column
    # concatenated into one: a table identified two different ways by two
    # different sources needs two constraints, and merging them produces one
    # that is weaker than either — and inert besides, since a NULL on one
    # side of a merged key makes the whole row unconstrained.
    #
    # Declared keys replace the derived one rather than joining it, so that
    # a table states its uniqueness in one place or the other, never both.
    for unique in table.unique_keys:
        if not unique.columns:
            continue  # V303 reports a key that names nothing
        cols = ", ".join(quoted(c.physical) for c in unique.columns)
        out.append(f"    UNIQUE ({cols})")

    # The uniqueness a dimension implies by declaring a natural key, when it
    # has not stated one itself. V302 requires that key and calls it what a
    # loader matches on; leaving it unenforced makes the claim exactly as
    # true as the comment above says an unchecked claim stays.
    if table.is_dimension and not table.unique_keys:
        derived = _derived_key(table)
        if derived:
            cols = ", ".join(quoted(c.physical) for c in derived)
            out.append(f"    UNIQUE ({cols})")

    return out


def _derived_key(table: Table) -> tuple[Column, ...]:
    """Derive what a dimension is unique on from its roles alone.

    Empty whenever the answer cannot be reached without guessing, and the
    two ways that happens are both already reported. Silence is the safe
    direction here: emitting a natural key alone on a table that turns out
    to version would reject the second version of every row, which is the
    constraint being wrong in the direction that looks like broken data.
    """
    if not table.natural_keys:
        return ()  # V302 reports it
    if table.scd in {"TYPE_0", "TYPE_1"}:
        # Neither keeps a second row for one business entity, so the natural
        # key is the whole of it.
        return table.natural_keys
    if table.scd == "TYPE_2":
        # A type-2 dimension's natural key repeats once per version, so the
        # uniqueness that applies to it is the natural key *plus* whatever
        # marks the versions apart.
        #
        # One discriminator, not both. Concatenating them weakens the very
        # constraint this exists to tighten: `UNIQUE (nk, start, number)`
        # permits two rows sharing a natural key and a start that differ
        # only in their counter, which either column alone would have
        # forbidden. Declaring more versioning metadata must not buy a
        # worse guarantee.
        #
        # The start wins when both are present, because a period is the more
        # specific claim — a counter orders versions, a start says when.
        # `is_current` is not a discriminator at all: it is true of exactly
        # one version, so a constraint carrying it is vacuous.
        marks = table.version_starts or table.version_numbers
        return (*table.natural_keys, marks[0]) if marks else ()  # V506
    return ()  # no varda:scd — V502 reports it, and guessing costs rows


def _table(table: Table, schema: str) -> str:
    """Render one CREATE TABLE, with its grain as a comment."""
    lines: list[str] = []
    if table.description:
        lines += _comment(table.description)
    if table.grain_statement:
        lines += _comment(table.grain_statement, "Grain: ")
    if table.scd:
        lines += _comment(table.scd, "Slowly-changing: ")
    lines.append(f"CREATE TABLE {quoted(schema)}.{quoted(table.physical)} (")

    body = [_column_line(c) for c in table.columns]
    for fk in table.foreign_keys:
        target = fk.table.model.table(fk.references or "")
        if target is None:
            continue  # V403 already reported it; do not also crash here
        key = target.surrogate_keys
        if not key:
            continue  # V301's business
        body.append(
            # REFERENCES on its own line, always rather than only when the
            # one-liner overflows. Quoting pushed the longer keys past the
            # width a terminal shows, and a format that changes shape with
            # the length of a name produces diffs that look meaningful and
            # are not.
            f"    FOREIGN KEY ({quoted(fk.physical)})\n"
            f"        REFERENCES {quoted(schema)}."
            f"{quoted(target.physical)} ({quoted(key[0].physical)})"
        )
    body += _unique_constraints(table)

    lines.append(",\n".join(body))
    lines.append(");")
    return "\n".join(lines)


def generate(model: DimensionalModel, schema: str = "mart") -> str:
    """Render the whole model as one runnable DDL script."""
    header = [
        "-- Generated by varda. Do not edit.",
        f"-- Source: {model.source.name}",
        "",
        f"CREATE SCHEMA IF NOT EXISTS {quoted(schema)};",
        "",
        "",
    ]
    blocks = [_table(t, schema) for t in _ordered(model)]
    return "\n".join(header) + "\n\n".join(blocks) + "\n"


def run(ctx: Context) -> dict[str, str]:
    """Run this generator and return its artifacts."""
    return {"sql/mart.sql": generate(ctx.model, ctx.schema)}
