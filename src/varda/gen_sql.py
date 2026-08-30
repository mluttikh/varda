"""SQL DDL generation.

Emits ``CREATE TABLE`` for every table in the model, in an order that lets the
whole file run against an empty database: every table follows the ones it
references, because a foreign key cannot be declared before its target exists.
Facts fall last on their own, since nothing may reference a fact.

How much of the model the database is asked to police is a level rather than
a given — see :data:`LEVELS`. The ordering above runs at every level, even
where no reference is emitted and nothing depends on it, so that changing the
level produces a diff about constraints and not about where the tables sit.

Nothing here is timestamped and nothing depends on the environment. That is
not a stylistic preference — a generated file that changes when nothing about
the model changed makes "is this output current?" unanswerable, and turns
every regeneration into a diff nobody reads.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
    # The aware form, spelled in full as `DOUBLE PRECISION` is: `TIMESTAMPTZ`
    # is PostgreSQL's own abbreviation for it, which DuckDB and Snowflake
    # both take, and the standard spelling says which of the two a reader is
    # looking at without their having to know that.
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
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
    generator. It carries the three ways the emitted file actually differs
    between the engines named here: what the types are called, how a schema
    is asked for, and whether a constraint can be stated without also being
    policed.

    Engines are added by being verified, not by being plausible. `bigquery`
    and `oracle` are absent for that reason and not by oversight: BigQuery
    has no `UNIQUE` constraint, and Oracle has no `CREATE SCHEMA` and no UUID
    type. Both need more than a type table, and half a dialect emits a file
    that looks right and does not run. BigQuery's other blocker was needing
    `NOT ENFORCED` on the keys it does have; that one is now a field.
    """

    name: str
    #: Ranges this dialect spells differently, overlaying :data:`TYPES`.
    types: Mapping[str, str] = field(default_factory=dict)
    #: The statement that creates the schema, given `{quoted}` and `{name}`.
    create_schema: str = CREATE_SCHEMA
    #: Whether an unsized string is safe to emit. False where a bare
    #: `VARCHAR` means something other than "as long as it needs to be".
    sizes_strings: bool = False
    #: How this dialect marks a constraint as declared but not policed, or
    #: empty where it cannot say it. A constraint carries two meanings — an
    #: assertion about the data, and an instruction to check every write —
    #: and only some engines separate them. Where this is empty, the only
    #: way to stop the checking is to stop emitting the constraint.
    unenforced: str = ""

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
    #
    # Snowflake enforces `NOT NULL` and nothing else: it records a primary,
    # unique or foreign key and never checks it. `RELY` is the difference
    # between recorded and trusted — the optimizer will eliminate a join
    # against a key marked this way, and will not against one that is not.
    # So the default emission stays bare, which is the honest rendering of
    # "I asked for enforcement and this engine does not enforce", and
    # `--constraints asserted` adds `RELY`, which is the operator saying the
    # loader guarantees it.
    "snowflake": Dialect("snowflake", unenforced="RELY"),
    "sqlserver": Dialect(
        "sqlserver",
        types={
            # `TIMESTAMP` in T-SQL is a synonym for `rowversion`: an
            # incrementing counter, not a time. Emitting it for `valid_from`
            # produces a type-2 dimension whose version period holds no
            # dates, and nothing anywhere reports it.
            "datetime": "DATETIME2",
            # T-SQL has no `WITH TIME ZONE` on any type; the offset-bearing
            # datetime is its own. Note that this is the one range where
            # `gen_sqlalchemy`'s generic type needs no correction here:
            # `sa.DateTime(timezone=True)` renders `DATETIMEOFFSET` of its
            # own accord, where the naive `sa.DateTime()` renders `DATETIME`.
            "timestamptz": "DATETIMEOFFSET",
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

#: How much of the model the database is asked to police, weakest last.
#:
#: `enforced` is the default and the right one: a claim the database checks
#: is a claim that stays true. It is not free — on the engines that really
#: enforce, the checking dominates a bulk load, and the cost falls almost
#: entirely on a fact table's grain and its foreign keys.
#:
#: `asserted` says the claims hold and the loader is what makes them hold.
#: Where a dialect can mark a constraint unenforced it is emitted and marked,
#: which keeps it in the catalog and available to the optimizer. Where it
#: cannot, the constraint is emitted as a comment instead, so the DDL still
#: records what the loader is promising.
#:
#: `none` emits bare tables. Column typing and `NOT NULL` stay at every
#: level: they are what a column *is* rather than a claim made about the
#: rows, they cost nothing measurable, and a `VARCHAR(20)` that stops being
#: twenty characters wide is a different schema rather than a faster one.
#:
#: Under the two weaker levels the claims do not disappear — `gen_assertions`
#: emits every one of them as a query that runs once per load instead of once
#: per row.
LEVELS = ("enforced", "asserted", "none")

#: The level assumed when none is named.
DEFAULT_LEVEL = "enforced"


class GenerationError(Exception):
    """A model cannot be generated from, and the run must stop."""


@dataclass(frozen=True)
class Enforcement:
    """What one level of :data:`LEVELS` means against one dialect.

    Resolved once per run rather than tested at each emission site, so that
    "what does `asserted` mean here" has exactly one answer and it is
    written down in one place.
    """

    #: Emit `PRIMARY KEY` on surrogate keys. Kept under `asserted` even
    #: where the rest is dropped: it is a fraction of the load cost, it is
    #: the identity of a dimension row rather than a claim about it, and
    #: every foreign key that names it needs it to exist — standard SQL
    #: requires a reference target to carry a primary or unique key, so a
    #: level that dropped it would have to drop those too and would be
    #: `none` under another name.
    keys: bool
    #: Emit `UNIQUE` for each of the table's unique claims.
    unique: bool
    #: Emit `FOREIGN KEY`.
    references: bool
    #: Appended to each constraint to mark it declared but not policed.
    suffix: str = ""
    #: Render the claims this level drops as comments, so the DDL still
    #: states what the loader is being trusted to hold.
    documented: bool = False


def enforcement(level: str, sql: Dialect) -> Enforcement:
    """Resolve a level against a dialect, or raise listing the levels.

    `asserted` means one thing — the claims hold, do not check them on every
    write — and reaches the DDL as whichever form the engine has for saying
    it. That is not a silent degradation: the header names the level, and
    where the engine has no way to mark a constraint the claims it drops are
    written into the file as comments. An engine that cannot say something
    is a reason to say it differently, not a reason to refuse.
    """
    if level not in LEVELS:
        known = ", ".join(LEVELS)
        msg = f"unknown constraint level {level!r}. Known levels: {known}"
        raise GenerationError(msg)
    if level == "enforced":
        return Enforcement(keys=True, unique=True, references=True)
    if level == "none":
        return Enforcement(keys=False, unique=False, references=False)
    if sql.unenforced:
        return Enforcement(
            keys=True,
            unique=True,
            references=True,
            suffix=f" {sql.unenforced}",
        )
    return Enforcement(
        keys=True, unique=False, references=False, documented=True
    )


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


def _by_dependency(
    tables: tuple[Table, ...], *, references: bool
) -> list[Table]:
    """Order tables so every reference target precedes the table using it.

    Sorting by name is not enough once one dimension references another,
    which is what a snowflake is: `DimCity` sorts before `DimState` and
    references it, so the emitted file creates a table whose target does not
    exist yet and stops there.

    Ties are broken by name, so a model with no dimension-to-dimension
    references emits in exactly the order it did before — a flat star sees no
    diff.

    The ordering runs whether or not foreign keys are emitted, so that
    turning enforcement off does not reshuffle a file and produce a diff
    that means nothing. Only the refusal is conditional: with no foreign
    keys in the output there is nothing an order could violate.
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
            # this raises rather than emitting a file that cannot run — the
            # same treatment an unmapped range gets.
            #
            # Only while they are being emitted. Under `--constraints
            # asserted` or `none` the file declares no references, nothing
            # in it depends on the order, and a pair of dimensions that
            # point at each other — which V404 permits — generates. The
            # remainder is emitted by name so the output stays
            # deterministic.
            if references:
                names = ", ".join(sorted(remaining))
                msg = (
                    f"foreign keys form a cycle among: {names}. "
                    f"No CREATE TABLE order satisfies them."
                )
                raise GenerationError(msg)
            ready = sorted(remaining)
        out += [remaining.pop(name) for name in ready]
    return out


def _ordered(model: DimensionalModel, *, references: bool) -> list[Table]:
    """Order tables so every foreign key's target is already created.

    Dimensions and bridges are ordered together rather than as two blocks.
    A bridge references dimensions, but a dimension may reference a bridge
    too — V404 permits both — and two fixed blocks cannot express that.
    Facts come last and need no ordering among themselves, because nothing
    may reference a fact.
    """
    return [
        *_by_dependency(
            (*model.dimensions, *model.bridges), references=references
        ),
        *model.facts,
    ]


def _column_line(column: Column, sql: Dialect, rule: Enforcement) -> str:
    """Render one column, with whatever the level lets it carry.

    `NOT NULL` is not conditional. It is not a claim about which rows go
    together, it costs nothing measurable on a load, and a column that
    quietly starts accepting nulls changes what every query against it
    means. A surrogate key that loses its `PRIMARY KEY` under `none` picks
    `NOT NULL` up instead, because half of what a primary key says is that
    the column is populated and that half is free.
    """
    parts = [f"    {quoted(column.physical)}", sql_type(column, sql)]
    if column.role == "SURROGATE_KEY":
        parts.append(f"PRIMARY KEY{rule.suffix}" if rule.keys else "NOT NULL")
    elif column.required:
        parts.append("NOT NULL")
    return " ".join(parts)


def _unique_line(columns: tuple[Column, ...], rule: Enforcement) -> str:
    """Render one uniqueness claim as a constraint."""
    names = ", ".join(quoted(c.physical) for c in columns)
    return f"UNIQUE ({names}){rule.suffix}"


def _reference_line(fk: Column, schema: str, rule: Enforcement) -> str | None:
    """Render one foreign key, or nothing where there is no target to name.

    REFERENCES goes on its own line, always rather than only when the
    one-liner overflows. Quoting pushed the longer keys past the width a
    terminal shows, and a format that changes shape with the length of a
    name produces diffs that look meaningful and are not.
    """
    target = fk.table.model.table(fk.references or "")
    if target is None:
        return None  # V403 already reported it; do not also crash here
    key = target.surrogate_keys
    if not key:
        return None  # V301's business
    return (
        f"FOREIGN KEY ({quoted(fk.physical)})\n"
        f"        REFERENCES {quoted(schema)}."
        f"{quoted(target.physical)} ({quoted(key[0].physical)}){rule.suffix}"
    )


def _claims(table: Table, schema: str, rule: Enforcement) -> list[str]:
    """Render every claim beyond the primary key, in model order.

    What the table says is unique first, then what it says it references.
    The uniqueness comes from :attr:`varda.model.Table.unique_claims`, which
    the assertion generator reads too — the DDL renders each claim as a
    constraint and the assertions render each as a query, and one claim
    computed in two places is one claim that eventually disagrees with
    itself.
    """
    out: list[str] = []
    if rule.unique:
        out += [_unique_line(c, rule) for c in table.unique_claims]
    if rule.references:
        out += [
            line
            for fk in table.foreign_keys
            if (line := _reference_line(fk, schema, rule)) is not None
        ]
    return out


def _dropped(table: Table, schema: str, rule: Enforcement) -> list[str]:
    """Say, in the file, which claims the level left to the loader.

    Only where the level dropped them and the dialect had no way to mark
    them. A claim that vanishes from the DDL without a word is a claim
    nobody knows is being made, and the DDL is what a DBA reads. Under
    `none` this is silent by design: that level asks for bare tables.
    """
    if not rule.documented:
        return []
    plain = Enforcement(keys=True, unique=True, references=True)
    claims = _claims(table, schema, plain)
    if not claims:
        return []
    # Each claim in the shape it would have had, one comment line per line
    # of it, so a reference still breaks before REFERENCES and the block
    # stays inside the width a terminal shows.
    return [
        "-- Not enforced here. The loader is trusted to hold:",
        *[f"--   {line}" for claim in claims for line in claim.splitlines()],
    ]


def _table(table: Table, schema: str, sql: Dialect, rule: Enforcement) -> str:
    """Render one CREATE TABLE, with its grain as a comment."""
    lines: list[str] = []
    if table.description:
        lines += _comment(table.description)
    if table.grain_statement:
        lines += _comment(table.grain_statement, "Grain: ")
    if table.scd:
        lines += _comment(table.scd, "Slowly-changing: ")
    lines.append(f"CREATE TABLE {quoted(schema)}.{quoted(table.physical)} (")

    body = [_column_line(c, sql, rule) for c in table.columns]
    body += [f"    {claim}" for claim in _claims(table, schema, rule)]

    lines.append(",\n".join(body))
    lines.append(");")
    lines += _dropped(table, schema, rule)
    return "\n".join(lines)


def generate(
    model: DimensionalModel,
    schema: str = "mart",
    dialect_name: str = DEFAULT_DIALECT,
    level: str = DEFAULT_LEVEL,
) -> str:
    """Render the whole model as one runnable DDL script."""
    sql = dialect(dialect_name)
    rule = enforcement(level, sql)
    name = quoted(schema)
    header = [
        "-- Generated by varda. Do not edit.",
        f"-- Source: {model.source.name}",
        f"-- Dialect: {sql.name}",
        # Named on every level, including the default. A header that says
        # which mode produced the file only when the mode is unusual is one
        # a reader cannot trust when it is silent.
        f"-- Constraints: {level}",
        "",
        sql.create_schema.format(
            quoted=name,
            literal=literal(schema),
            exec_body=literal(f"CREATE SCHEMA {name}"),
        ),
        "",
        "",
    ]
    tables = _ordered(model, references=rule.references)
    blocks = [_table(t, schema, sql, rule) for t in tables]
    return "\n".join(header) + "\n\n".join(blocks) + "\n"


def run(ctx: Context) -> dict[str, str]:
    """Run this generator and return its artifacts."""
    return {
        "sql/mart.sql": generate(
            ctx.model, ctx.schema, ctx.dialect, ctx.constraints
        )
    }
