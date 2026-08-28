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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .model import ONE_IDENTITY

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .ext import Context
    from .model import Column, DimensionalModel, Table

#: LinkML range to SQL type, as PostgreSQL spells it. Deliberately a closed
#: table with no fallback: an unmapped range raises rather than quietly
#: becoming TEXT, because a column silently typed as text is a bug that
#: surfaces years later as a comparison that does not do what it looks like.
#:
#: PostgreSQL rather than an imagined neutral SQL, because there is no such
#: thing and pretending otherwise is how `TIMESTAMP` came to be emitted for a
#: type-2 dimension's `valid_from`: in T-SQL that names a row-version counter
#: with no date in it, so every SCD rule in this package was certifying a
#: column that would hold something else entirely. A dialect that is named can
#: be checked; one that is assumed cannot.
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
    "uuid": "UUID",
    "uri": "VARCHAR",
    "uriorcurie": "VARCHAR",
    "ncname": "VARCHAR",
}

#: How a dialect asks for a schema, given the placeholders :func:`generate`
#: fills: `{quoted}` is the schema name as an identifier, `{literal}` is it as
#: a string, and `{exec_body}` is the whole `CREATE SCHEMA` wrapped as one.
#: A template uses the ones it needs and ignores the rest.
#:
#: PostgreSQL's form is the common one and DuckDB and Snowflake both take it.
CREATE_SCHEMA = "CREATE SCHEMA IF NOT EXISTS {quoted};"


@dataclass(frozen=True)
class Dialect:
    """One database's spelling of the DDL Varda emits.

    A narrow record on purpose. This is not an abstraction over SQL — that is
    a transpiler, and a transpiler is a much larger thing to own than a
    generator. It carries the two ways the emitted file actually differs
    between the engines named here: what the types are called, and how a
    schema is asked for.

    Engines are added by being verified, not by being plausible. `bigquery`
    and `oracle` are absent for that reason and not by oversight: BigQuery has
    no `UNIQUE` constraint and needs `NOT ENFORCED` on the keys it does have,
    and Oracle has no `CREATE SCHEMA` and no UUID type. Both need more than a
    type table, and half a dialect emits a file that looks right and does not
    run.
    """

    name: str
    #: Ranges this dialect spells differently, overlaying :data:`TYPES`.
    types: Mapping[str, str] = field(default_factory=dict)
    #: The statement that creates the schema, given `{quoted}` and `{name}`.
    create_schema: str = CREATE_SCHEMA
    #: Whether an unsized string is safe to emit. False where a bare
    #: `VARCHAR` means something other than "as long as it needs to be".
    sizes_strings: bool = False

    def type_of(self, rng: str) -> str | None:
        """Give this dialect's name for a range, or ``None`` if it has none."""
        return self.types.get(rng, TYPES.get(rng))


DIALECTS: dict[str, Dialect] = {
    # The base table is PostgreSQL's, so this overlays nothing. Named anyway,
    # because "the default" and "PostgreSQL" being the same string is the
    # claim being made and a reader should be able to see it.
    "postgres": Dialect("postgres"),
    # Accepts every spelling in the base table, `DOUBLE PRECISION` and `UUID`
    # included, and is the engine the test suite executes against.
    "duckdb": Dialect("duckdb"),
    # Also accepts the base table. `UUID` is a real type here as of its
    # general availability, so a key that arrives as one stays 16 bytes
    # rather than becoming a 36-character string.
    "snowflake": Dialect("snowflake"),
    "sqlserver": Dialect(
        "sqlserver",
        types={
            # `TIMESTAMP` in T-SQL is a synonym for `rowversion`: an
            # incrementing counter, not a time. Emitting it for `valid_from`
            # produces a type-2 dimension whose version period holds no
            # dates, and nothing anywhere reports it.
            "datetime": "DATETIME2",
            # There is no boolean type; `BIT` is what everyone uses.
            "boolean": "BIT",
            "float": "FLOAT",
            "double": "FLOAT",
            "uuid": "UNIQUEIDENTIFIER",
        },
        # `CREATE SCHEMA` must be the first statement in its batch, so it
        # cannot be guarded by an `IF` directly and goes through `EXEC`.
        # Both halves are string literals, and the second holds a whole
        # statement — so the name is escaped once for the test and the DDL
        # is escaped again for the wrapper. That is why the placeholders are
        # pre-rendered rather than being the bare name: one template cannot
        # express two depths of quoting.
        create_schema="IF SCHEMA_ID({literal}) IS NULL EXEC({exec_body});",
        sizes_strings=True,
    ),
}

#: The dialect assumed when none is named. Today's output, now under its own
#: name rather than under a claim of neutrality.
DEFAULT_DIALECT = "postgres"


class GenerationError(Exception):
    """A model cannot be generated from, and the run must stop."""


def dialect(name: str) -> Dialect:
    """Look one dialect up by name, or raise listing the ones there are."""
    found = DIALECTS.get(name)
    if found is None:
        known = ", ".join(sorted(DIALECTS))
        msg = f"unknown dialect {name!r}. Known dialects: {known}"
        raise GenerationError(msg)
    return found


def sql_type(column: Column, sql: Dialect) -> str:
    """Map a column's range to a SQL type, or raise naming the column.

    Resolved through the range's own type chain, nearest first, so a schema
    declaring ``money: {typeof: decimal}`` gets `NUMERIC` rather than
    stopping the generator. That used to be the worst of the two answers a
    model can get: `varda check --strict` reported nothing and `varda
    generate` refused, and the first one is the one people trust.

    Nearest first is what keeps `uuid` a `UUID`. It is declared
    ``typeof: string``, so a chain walked the other way would find `VARCHAR`
    and hand back a 36-character column that sorts and compares as text.
    Every entry in :data:`TYPES` is now reachable this way, which makes the
    `uuid` row an instance of the rule rather than an exception to it.
    """
    mapped = next(
        (t for rng in column.type_chain if (t := sql.type_of(rng)) is not None),
        None,
    )
    if mapped is None:
        known = ", ".join(sorted(TYPES))
        tried = " -> ".join(column.type_chain)
        msg = (
            f"{column}: range {column.range!r} has no SQL mapping "
            f"(tried {tried}). Known ranges: {known}"
        )
        raise GenerationError(msg)
    if sql.sizes_strings and mapped == "VARCHAR" and column.max_length is None:
        msg = (
            f"{column}: a string column needs varda:max_length under "
            f"--dialect {sql.name}, where a VARCHAR with no length is a "
            f"VARCHAR(1) and silently truncates every value to one character"
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


def literal(text: str) -> str:
    """Render one value so SQL reads it as a string and nothing else.

    The companion to :func:`quoted`, and it exists for the same reason one
    layer over. Quoting settles identifiers; nothing settled the string
    literals, and one dialect needs them: T-SQL cannot guard `CREATE SCHEMA`
    with an `IF` directly, so it tests `SCHEMA_ID('...')` and runs the DDL
    through `EXEC('...')`. Both are strings holding a schema name that
    arrives from `--schema` unexamined. A name with an apostrophe in it
    emitted DDL no database would parse, and did so only under the one
    dialect the test suite cannot execute.

    An embedded quote is doubled, following SQL:2016 — the same rule as
    identifiers, against the other quote character.
    """
    return "'" + text.replace("'", "''") + "'"


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


def _column_line(column: Column, sql: Dialect) -> str:
    parts = [f"    {quoted(column.physical)}", sql_type(column, sql)]
    if column.role == "SURROGATE_KEY":
        parts.append("PRIMARY KEY")
    elif column.required:
        parts.append("NOT NULL")
    return " ".join(parts)


def _unique_constraints(table: Table) -> list[str]:
    """Render the uniqueness constraints that apply to one table.

    Each distinct combination once. A grain and a declared `unique_keys`
    entry may name the same columns — one is Varda's vocabulary and one is
    LinkML's, and writing both is a reasonable thing to do — and every
    database accepts the doubled constraint by building a second index for
    it. Two indexes maintained on every write, for one claim, and nothing
    anywhere says so.
    """
    claims: list[tuple[Column, ...]] = []

    # The grain, as a constraint the database enforces rather than a comment
    # it ignores. This is the whole return on declaring `varda:grain` as
    # columns: a uniqueness claim nobody checks is a uniqueness claim that
    # stops being true, quietly, on some load nobody is watching.
    if table.grain_columns:
        claims.append(table.grain_columns)

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
        claims.append(unique.columns)

    # The uniqueness a dimension implies by declaring a natural key, when it
    # has not stated one itself. V302 requires that key and calls it what a
    # loader matches on; leaving it unenforced makes the claim exactly as
    # true as the comment above says an unchecked claim stays.
    if table.is_dimension and not table.unique_keys:
        derived = _derived_key(table)
        if derived:
            claims.append(derived)

    # Ordered, not sorted: the grain leads because it is the table's own
    # statement of what a row is, and a reader comparing the DDL against the
    # model should meet them in the order the model declares them.
    out: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for columns in claims:
        names = tuple(c.physical for c in columns)
        if names in seen:
            continue
        seen.add(names)
        out.append(f"    UNIQUE ({', '.join(quoted(n) for n in names)})")
    return out


def _derived_key(table: Table) -> tuple[Column, ...]:
    """Derive what a dimension is unique on from its roles alone.

    Empty whenever the answer cannot be reached without guessing, and the
    three ways that happens are all reported elsewhere. Silence is the safe
    direction here: emitting a natural key alone on a table that turns out
    to version would reject the second version of every row, which is the
    constraint being wrong in the direction that looks like broken data.

    Two natural keys are the third way, and the one that reads as though it
    needed no guess. They mean either one compound identity — a store known
    by its chain and its number — or two alternative ones, a product carrying
    a barcode from one source and a supplier's part number from another. A
    role cannot tell those apart, and the two want opposite constraints: one
    over both columns, or one over each. Emitting the merged form for a table
    that meant the second is worse than emitting nothing, because it is
    weaker than either key alone and a NULL on one side leaves the row
    unconstrained entirely. V306 asks the model to say which.
    """
    if not table.natural_keys:
        return ()  # V302 reports it
    if len(table.natural_keys) > ONE_IDENTITY:
        return ()  # V306 reports it
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


def _table(table: Table, schema: str, sql: Dialect) -> str:
    """Render one CREATE TABLE, with its grain as a comment."""
    lines: list[str] = []
    if table.description:
        lines += _comment(table.description)
    if table.grain_statement:
        lines += _comment(table.grain_statement, "Grain: ")
    if table.scd:
        lines += _comment(table.scd, "Slowly-changing: ")
    lines.append(f"CREATE TABLE {quoted(schema)}.{quoted(table.physical)} (")

    body = [_column_line(c, sql) for c in table.columns]
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


def generate(
    model: DimensionalModel,
    schema: str = "mart",
    dialect_name: str = DEFAULT_DIALECT,
) -> str:
    """Render the whole model as one runnable DDL script."""
    sql = dialect(dialect_name)
    name = quoted(schema)
    header = [
        "-- Generated by varda. Do not edit.",
        f"-- Source: {model.source.name}",
        f"-- Dialect: {sql.name}",
        "",
        sql.create_schema.format(
            quoted=name,
            literal=literal(schema),
            exec_body=literal(f"CREATE SCHEMA {name}"),
        ),
        "",
        "",
    ]
    blocks = [_table(t, schema, sql) for t in _ordered(model)]
    return "\n".join(header) + "\n\n".join(blocks) + "\n"


def run(ctx: Context) -> dict[str, str]:
    """Run this generator and return its artifacts."""
    return {"sql/mart.sql": generate(ctx.model, ctx.schema, ctx.dialect)}
