"""SQLAlchemy Core table definitions.

A ``MetaData`` and one ``sa.Table`` per table. Core, not the ORM: a star
schema has no object graph to map, its rows are loaded in bulk and read in
aggregate, and the identity map, unit of work and lazy loading are costs with
nothing on the other side. Core is also the layer that matches what Varda
already knows — a name, columns, types and constraints — so this and
:mod:`varda.gen_sql` are two renderings of one model rather than two models.

Worth more than a second rendering, for two reasons that only exist here.

``sa.Column`` and ``sa.Table`` both take an ``info`` mapping that SQLAlchemy
stores and never interprets, so the annotations reach runtime. A consumer
holding the emitted module reads `SEMI_ADDITIVE over date_key` off a column
and refuses the sum while the query is still being built, with no Varda
installed. Until now `varda:additivity` and `varda:semi_additive_over` reached
no machine-readable output at all: `rules.py` calls an unclassified measure
the most expensive error a dimensional model produces, checks it, and then
the DDL gives the semi-additive balance and the additive quantity the same
`NUMERIC` and records the difference nowhere.

``comment`` becomes a real ``COMMENT ON TABLE`` / ``COMMENT ON COLUMN`` when
the metadata is created, which is where `information_schema` and every BI
tool look. The DDL writes descriptions as `--` comments and the parser
discards them.

The emitted module imports nothing of Varda's and is what `ruff format` would
produce at 79 columns — generated output is read by people, and a module that
reformats on its reader's first commit produces a diff nobody asked for.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from .gen_sql import DEFAULT_LEVEL, Dialect, GenerationError, enforcement

if TYPE_CHECKING:
    from .ext import Context
    from .gen_sql import Enforcement
    from .model import Column, DimensionalModel, Table

#: LinkML range to the SQLAlchemy type that renders it. The keys are
#: :data:`varda.gen_sql.TYPES`'s keys and a test says so, because two lists of
#: the ranges Varda knows are two answers to one question.
#:
#: Generic types only, and no dialect ever appears in the emitted module.
#: That is the point of the artifact: one definition every engine reads,
#: where `gen_sql` deliberately writes one dialect at a time.
#:
#: It is also the same delegation this generator already makes about
#: identifiers. SQLAlchemy quotes what each engine needs quoted and leaves
#: the rest bare, and Varda does not second-guess it, because that list is
#: maintained per dialect by people who do it for a living — the argument
#: `sqlglot` is already trusted on. What a type means on each engine is the
#: same kind of knowledge, kept by the same people. Pinning
#: `mssql.DATETIME2()` into every module to correct one of them would be
#: taking half that judgment back while leaving the rest.
#:
#: Where Varda does have an opinion an engine needs and SQLAlchemy's generic
#: type does not carry — `DATETIME2` rather than `DATETIME` on SQL Server —
#: it is in `sql/mart.sql` under `--dialect sqlserver`, which is the artifact
#: for creating tables on a named engine. The module header says so.
#:
#: `float` maps to `sa.Double` rather than `sa.Float` because `TYPES` maps it
#: to `DOUBLE PRECISION`. That is not a dialect opinion — it is which generic
#: type renders the decision `TYPES` already made.
TYPES = {
    "string": "sa.String",
    "integer": "sa.Integer",
    "float": "sa.Double",
    "double": "sa.Double",
    "decimal": "sa.Numeric",
    "boolean": "sa.Boolean",
    "date": "sa.Date",
    "datetime": "sa.DateTime",
    "time": "sa.Time",
    "uuid": "sa.Uuid",
    "uri": "sa.String",
    "uriorcurie": "sa.String",
    "ncname": "sa.String",
}

#: The width the emitted module is wrapped to, matching the house rule the
#: rest of the generated output follows.
LINE_LIMIT = 80

#: Annotations copied onto ``sa.Column(info=...)``, in this order.
COLUMN_INFO = (
    "role",
    "references",
    "additivity",
    "semi_additive_over",
    "unit",
)


def sa_type(column: Column) -> str:
    """Render a column's type as the SQLAlchemy expression that builds it.

    Resolved through the range's own type chain, nearest first, exactly as
    :func:`varda.gen_sql.sql_type` resolves it — a declared
    ``money: {typeof: decimal}`` reaches `sa.Numeric` the same way it reaches
    `NUMERIC`.

    No dialect is consulted and none can be. What each engine makes of a
    generic type is SQLAlchemy's to know, and the module is worth having
    because it is the one artifact here that does not pick an engine.
    """
    base = next((TYPES[r] for r in column.type_chain if r in TYPES), None)
    if base is None:
        known = ", ".join(sorted(TYPES))
        tried = " -> ".join(column.type_chain)
        msg = (
            f"{column}: range {column.range!r} has no SQLAlchemy type "
            f"(tried {tried}). Known ranges: {known}"
        )
        raise GenerationError(msg)
    return f"{base}({_args(column)})"


def _args(column: Column) -> str:
    """Render the type's parameters, on the same rules the DDL uses."""
    if column.max_length is not None:
        return str(column.max_length)
    precision = column.precision
    if precision is None:
        return ""
    scale = column.scale
    if scale is None or scale > precision:
        return str(precision)
    return f"{precision}, {scale}"


def _quote(value: str) -> str:
    """Render a string as a double-quoted literal.

    Double quotes rather than :func:`repr`, which prefers single ones: the
    emitted module has to be what `ruff format` would produce, and it would
    rewrite every one of them.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _text(value: str, indent: int) -> str:
    """Render a string that may be too long for one line.

    Wrapped into implicit concatenation inside the parentheses the argument
    already has, which is the shape `ruff format` produces and leaves alone.
    A description is prose and runs past the limit often; truncating it would
    drop the half of the model written for a person.
    """
    pad = " " * indent
    room = LINE_LIMIT - indent - len("comment=(")
    lines = textwrap.wrap(value, width=max(room, 20)) or [""]
    if len(lines) == 1 and indent + len(lines[0]) + 12 <= LINE_LIMIT:
        return _quote(lines[0])
    joined = [
        f"{line} " if i < len(lines) - 1 else line
        for i, line in enumerate(lines)
    ]
    body = "\n".join(f"{pad}    {_quote(part)}" for part in joined)
    return f"(\n{body}\n{pad})"


def _mapping(pairs: list[tuple[str, object]], indent: int) -> str:
    """Render an ``info`` mapping, or nothing when there is nothing to say.

    Always exploded, one key per line, because a trailing comma is what makes
    `ruff format` leave a collection alone — the alternative is matching its
    rules for when a line fits, which is a second formatter to maintain.
    """
    kept = [(k, v) for k, v in pairs if v not in (None, (), [], "")]
    if not kept:
        return ""
    pad = " " * indent
    body = []
    for key, value in kept:
        head = f"{pad}    {_quote(key)}: "
        body.append(f"{head}{_value(value, len(head), indent + 4)},")
    return f"{pad}info={{\n" + "\n".join(body) + f"\n{pad}}},"


def _value(value: object, column: int, indent: int) -> str:
    """Render one ``info`` value, wrapping a collection that will not fit.

    ``column`` is where the value starts on its line and decides whether it
    fits; ``indent`` is the block it belongs to and decides where its parts
    go when it does not. The two differ for every nested value, and using the
    first for both is what indents a drill path off the right of the page.

    A collection is written flat while the line fits and exploded one element
    per line when it does not, which is what `ruff format` does with the same
    input. The two hierarchies of a date dimension are what force it.
    """
    if isinstance(value, str):
        return _quote(value)
    if not isinstance(value, (list, dict)):
        return repr(value)
    flat_parts, open_, close = _parts(value, column + 1, indent)
    flat = open_ + ", ".join(flat_parts) + close
    if column + len(flat) + 1 <= LINE_LIMIT:
        return flat
    parts, _, _ = _parts(value, indent + 4, indent + 4)
    pad = " " * indent
    inner = "\n".join(f"{pad}    {part}," for part in parts)
    return f"{open_}\n{inner}\n{pad}{close}"


def _parts(
    value: object, column: int, indent: int
) -> tuple[list[str], str, str]:
    """Split a collection into its rendered elements and its brackets."""
    if isinstance(value, dict):
        return (
            [
                f"{_quote(str(k))}: "
                f"{_value(v, column + len(str(k)) + 4, indent)}"
                for k, v in value.items()
            ],
            "{",
            "}",
        )
    items = list(value) if isinstance(value, list) else []
    return [_value(v, column, indent) for v in items], "[", "]"


def _hierarchies(table: Table) -> list[dict[str, object]]:
    """Render the drill paths as plain data a consumer can walk."""
    return [
        {"name": h.name, "levels": list(h.levels)}
        for h in table.hierarchies
        if h.name
    ]


def _column(column: Column, rule: Enforcement) -> str:
    """Render one ``sa.Column``, one argument per line."""
    lines = [
        "    sa.Column(",
        f"        {_quote(column.physical)},",
        f"        {sa_type(column)},",
    ]
    target = (
        column.table.model.table(column.references or "")
        if column.references
        else None
    )
    if rule.references and target is not None and target.surrogate_keys:
        key = target.surrogate_keys[0]
        ref = _quote(f"{target.physical}.{key.physical}")
        lines.append(f"        sa.ForeignKey({ref}),")
    if column.role == "SURROGATE_KEY" and rule.keys:
        # autoincrement=False or SQLAlchemy emits SERIAL / IDENTITY: it reads
        # an integer primary key as one the database generates. A warehouse
        # surrogate key is assigned by the loader, and a sequence default
        # would quietly fill in for a load that left it null.
        lines.append("        primary_key=True,")
        lines.append("        autoincrement=False,")
    else:
        # A surrogate key that has lost its primary key keeps `NOT NULL`,
        # which is the half of what a primary key says that costs nothing —
        # `gen_sql._column_line` makes the same trade, and a model whose
        # surrogate keys are not declared `required` is what tells the two
        # apart.
        surrogate = column.role == "SURROGATE_KEY"
        lines.append(f"        nullable={not (surrogate or column.required)},")
    if column.description:
        lines.append(f"        comment={_text(column.description, 8)},")
    info = _mapping([(k, getattr(column, k)) for k in COLUMN_INFO], 8)
    if info:
        lines.append(info)
    lines.append("    ),")
    return "\n".join(lines)


def _table(table: Table, rule: Enforcement) -> str:
    """Render one ``sa.Table``."""
    lines = [
        f"{table.physical} = sa.Table(",
        f"    {_quote(table.physical)},",
        "    metadata,",
    ]
    lines += [_column(c, rule) for c in table.columns]
    if rule.unique:
        for claim in table.unique_claims:
            cols = ", ".join(_quote(c.physical) for c in claim)
            lines.append(f"    sa.UniqueConstraint({cols}),")
    if table.description:
        lines.append(f"    comment={_text(table.description, 4)},")
    # What the level dropped, said in the one place this module can say it.
    # The DDL writes the same claims into a comment block; here they are
    # data, so a consumer can still read what the loader is trusted to hold.
    dropped: list[list[str]] = []
    if rule.documented:
        dropped = [[c.physical for c in claim] for claim in table.unique_claims]
    info = _mapping(
        [
            ("role", table.role),
            ("grain", list(table.grain)),
            ("unique_unenforced", dropped),
            ("grain_statement", table.grain_statement),
            ("scd", table.scd),
            ("fact_type", table.fact_type),
            ("hierarchies", _hierarchies(table)),
        ],
        4,
    )
    if info:
        lines.append(info)
    lines.append(")")
    return "\n".join(lines)


def generate(
    model: DimensionalModel,
    schema: str = "mart",
    level: str = DEFAULT_LEVEL,
) -> str:
    """Render the whole model as one SQLAlchemy Core module."""
    # A dialect with no way to mark a constraint unenforced, which is the
    # truth about this layer: SQLAlchemy has no `RELY`. So `asserted` reads
    # here the way it reads on an engine that enforces — the primary keys
    # stay, the rest of the claims move into `info` rather than being
    # declared — and that reading is the same on every engine, which is what
    # a database-neutral module needs it to be.
    rule = enforcement(level, Dialect("sqlalchemy"))
    # Dimensions, then bridges, then facts — the order the DDL emits, so the
    # two files read side by side. Nothing here needs it: SQLAlchemy resolves
    # a `ForeignKey` string lazily and `metadata.sorted_tables` orders
    # `create_all` itself, so a cycle that stops the DDL does not stop this.
    tables = [*model.dimensions, *model.bridges, *model.facts]
    blocks = [_table(t, rule) for t in tables]
    header = [
        '"""Generated by varda. Do not edit.',
        "",
        f"Source: {model.source.name}",
        f"Constraints: {level}",
        "",
        "Database-neutral: the types are SQLAlchemy's generic ones and no",
        "dialect is named, so one module serves every engine. Where an",
        "engine wants a spelling a generic type does not produce —",
        "DATETIME2 rather than DATETIME on SQL Server — sql/mart.sql under",
        "--dialect sqlserver is where that decision lives.",
        '"""',
        "",
        "import sqlalchemy as sa",
        "",
        f"metadata = sa.MetaData(schema={_quote(schema)})",
        "",
        "",
    ]
    return "\n".join(header) + "\n\n\n".join(blocks) + "\n"


def run(ctx: Context) -> dict[str, str]:
    """Run this generator and return its artifacts.

    ``ctx.dialect`` is deliberately unread. `Context` says a generator
    emitting something other than DDL is free to ignore it, and this is that
    generator: naming an engine is what the DDL is for.
    """
    return {"python/mart.py": generate(ctx.model, ctx.schema, ctx.constraints)}
