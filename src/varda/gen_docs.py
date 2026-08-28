"""Documentation generation.

Emits one Markdown reference for the whole model. The audience is an analyst
deciding whether a table answers their question, so the ordering is facts
first — a fact is what someone starts from — and the grain sentence is given
the most prominent position it can have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ext import Context
    from .model import DimensionalModel, Hierarchy, Level, Table

#: Below this, a level identifies itself and there is nothing to qualify.
IDENTITY_IS_COMPOUND = 2

ADDITIVITY = {
    "ADDITIVE": "sums across every dimension",
    "SEMI_ADDITIVE": "does not sum across",
    "NON_ADDITIVE": "never sum; recompute from components",
}


def _columns(table: Table) -> list[str]:
    rows = [
        "| Column | Role | Type | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for column in table.columns:
        notes: list[str] = []
        if column.references:
            notes.append(f"→ `{column.references}`")
        if column.additivity:
            phrase = ADDITIVITY.get(column.additivity, column.additivity)
            if column.additivity == "SEMI_ADDITIVE" and (
                column.semi_additive_over
            ):
                phrase = f"{phrase} `{column.semi_additive_over}`"
            notes.append(phrase)
        if column.unit:
            notes.append(f"unit: `{column.unit}`")
        # The width, for the person writing the query rather than the one
        # reading the DDL. Spelled out rather than shown as `VARCHAR(80)`,
        # because this page describes the model and the SQL type is one
        # generator's rendering of it.
        if column.max_length is not None:
            notes.append(f"at most {column.max_length} characters")
        if column.precision is not None:
            kept = f"{column.precision} digits"
            if column.scale is not None:
                kept += f", {column.scale} after the point"
            notes.append(kept)
        if column.description:
            notes.append(column.description)
        rows.append(
            f"| `{column.physical}` | {column.role or '—'} | "
            f"{column.range} | {'; '.join(notes) or '—'} |"
        )
    return rows


def _level(level: Level) -> str:
    """Render one drill-path step.

    Falls back to what was written when the level does not resolve, so a
    model that has not passed `check` still documents rather than raising.
    """
    if level.column is None:
        return f"`{level.spec}`"
    if level.via is not None and level.via.references:
        return f"`{level.via.references}.{level.column.name}`"
    return f"`{level.column.name}`"


def _identity_note(hierarchy: Hierarchy) -> str | None:
    """Say what the finest level needs beside it to name one member.

    The concepts page argues that what names a level is not what identifies
    it — `city_name` holds "Springfield" for cities in three states — and
    the drill path, which is a list of names, cannot show that. Nothing else
    read `Level.identity`, so the model computed the answer on every access
    and told no one.

    The identity is rendered whole rather than as the finest key plus what
    qualifies it, which reads wrong wherever a level declares a key: the
    merchandise path drills `brand → product_name` and is identified by
    `brand, gtin`, so naming `gtin` as the thing needing qualification put a
    column in the sentence that the path above it never mentions. The tuple
    is the answer, and stating it needs no cases.

    Only the finest level. Every coarser one's identity is a prefix of it,
    so listing them all repeats the same path at increasing lengths.
    """
    if not hierarchy.resolved:
        return None
    identity = hierarchy.resolved[-1].identity
    if len(identity) < IDENTITY_IS_COMPOUND:
        return None  # the level identifies itself; nothing to qualify
    named = ", ".join(f"`{c.name}`" for c in identity)
    return (
        f"one member is {named} together — a name at one level "
        f"repeats under a different parent"
    )


def _table(table: Table) -> str:
    lines = [f"### {table.name}", ""]
    if table.description:
        lines += [table.description, ""]
    facts = [f"**Physical name:** `{table.physical}`"]
    # The sentence leads and the columns follow, because the audience for
    # this page is deciding whether a table answers their question and the
    # sentence is the part that tells them. The columns matter to whoever
    # writes the query afterwards, which is why they get their own label
    # rather than being folded into the same line.
    if table.grain_statement:
        facts.append(f"**Grain:** {table.grain_statement}")
    if table.grain:
        cols = ", ".join(f"`{c}`" for c in table.grain)
        facts.append(f"**Unique on:** {cols}")
    if table.fact_type:
        facts.append(f"**Fact type:** {table.fact_type}")
    if table.scd:
        facts.append(f"**Slowly-changing:** {table.scd}")
    # Rendered coarsest-first with arrows, which is the direction a reader
    # drills rather than the direction the columns happen to be declared in.
    # A level reached through a foreign key is shown qualified by the table
    # it came from, because `country_name` alone leaves a reader looking for
    # a column this table does not have.
    for hierarchy in table.hierarchies:
        path = " → ".join(_level(lv) for lv in hierarchy.resolved)
        line = f"**Drill path** ({hierarchy.name}): {path}"
        if hierarchy.description:
            line = f"{line} — {hierarchy.description}"
        facts.append(line)
        note = _identity_note(hierarchy)
        if note:
            facts.append(f"**Unique within the path:** {note}")
    lines += [*facts, "", *_columns(table), ""]
    return "\n".join(lines)


def generate(model: DimensionalModel) -> str:
    """Render the model as a Markdown reference."""
    out = [
        "# Model reference",
        "",
        "Generated by varda. Do not edit.",
        "",
        f"Source: `{model.source.name}`",
        "",
    ]
    for heading, tables in (
        ("Facts", model.facts),
        ("Dimensions", model.dimensions),
        ("Bridges", model.bridges),
    ):
        if not tables:
            continue
        out += [f"## {heading}", ""]
        out += [_table(t) for t in tables]
    return "\n".join(out).rstrip() + "\n"


def run(ctx: Context) -> dict[str, str]:
    """Run this generator and return its artifacts."""
    return {"docs/model.md": generate(ctx.model)}
