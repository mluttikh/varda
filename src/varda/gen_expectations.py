"""Expectation generation — what the model says about its data, as queries.

`assertions.sql` holds the claims a constraint could have enforced, and
returns nothing exactly when the dropped constraints would have held. This
file holds the claims no constraint can state: that a type-2 dimension's
history is sound, and that a hierarchy rolls up the way it says. They are
the model's claims and not Varda's — each follows from roles and
hierarchies the model already declares — and they live in their own file
so that `assertions.sql` keeps the one meaning it has.

What the model claims is :func:`varda.claims.implied`, and what an active
extension adds through :attr:`varda.ext.Extension.claims`; this module only
renders. Each query returns the rows breaking its claim, and nothing when
it holds.
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

A plain level is checked the same way where its hierarchy is declared
`strict`, which says each level's column identifies a member on its own.

**A hierarchy is complete unless it says otherwise.** A row holding a level
holds the level above it, or it drops out of every roll-up through that
level. Not checked where the column above is required, which the DDL
already states, and not for a hierarchy declared `complete: false`, whose
rows may skip a level.

**No roll-up for a type-2 dimension's hierarchies.** Its rows are versions,
and a city moving region is history rather than a contradiction. Checking
each version on its own would need the current row, which only some designs
mark. Completeness is checked there all the same: a version missing its
category is as broken as a row.

Nulls follow SQL's reading, as in the assertions: a row whose key is null
is not part of any claim about that key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from . import analysis, claims
from .claims import (
    AtMostOneFlagged,
    Determines,
    FlaggedIsLatest,
    PeriodNonEmpty,
    PeriodsDisjoint,
    Requires,
)
from .ext import ClaimError
from .gen_assertions import LABEL
from .gen_sql import quoted
from .snapshot import Column, Hierarchy, Table, UniqueKey, table_of

if TYPE_CHECKING:
    from .claims import Claim
    from .ext import Context
    from .snapshot import Element, Model

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


def _where(subject: Element) -> str:
    """Name what a claim is about the way its label shows it."""
    table = table_of(subject)
    match subject:
        case Table():
            return table.physical
        case Column():
            return f"{table.physical}.{subject.physical}"
        case Hierarchy() | UniqueKey():
            return f"{table.physical}.{subject.name}"
        case _:
            assert_never(subject)


# ---------------------------------------------------------------------------
# One query per shape
# ---------------------------------------------------------------------------


def _empty_periods(claim: PeriodNonEmpty, relation: str) -> list[str]:
    """Render the rows whose period holds no instant."""
    start, end = quoted(claim.start.physical), quoted(claim.end.physical)
    return [
        'SELECT count(*) AS "empty_versions"',
        f"FROM {relation}",
        f"WHERE {end} <= {start};",
    ]


def _overlaps(claim: PeriodsDisjoint, table: Table, schema: str) -> list[str]:
    """Render the rows sharing a key whose periods are in force at once.

    `b` starts after `a` and before `a` ends, or `a` never ends. Strictly
    after: two starting together break a unique key, which says so plainly.
    """
    s, e = quoted(claim.start.physical), quoted(claim.end.physical)
    join = _same(claim.key, "a", "b")
    return [
        f'SELECT {_names(claim.key, "a")}, count(*) AS "overlaps"',
        f"FROM {_relation(table, schema, 'a')}",
        f"JOIN {_relation(table, schema, 'b')}",
        f"  ON {join[0]}",
        *(f" AND {line}" for line in join[1:]),
        f' AND "b".{s} > "a".{s}',
        f' AND ("a".{e} IS NULL OR "a".{e} > "b".{s})',
        f"GROUP BY {_names(claim.key, 'a')};",
    ]


def _one_flagged(claim: AtMostOneFlagged, relation: str) -> list[str]:
    """Render the keys more than one of whose rows is flagged."""
    names = _names(claim.key)
    return [
        f'SELECT {names}, count(*) AS "current_versions"',
        f"FROM {relation}",
        f"WHERE {_flagged(claim.flag)}",
        *(line.replace("WHERE", "  AND", 1) for line in _present(claim.key)),
        f"GROUP BY {names}",
        "HAVING count(*) > 1;",
    ]


def _latest(claim: FlaggedIsLatest, table: Table, schema: str) -> list[str]:
    """Render the flagged rows that a later row sharing their key follows."""
    m = quoted(claim.order.physical)
    join = _same(claim.key, "cur", "later")
    return [
        'SELECT count(*) AS "superseded_current"',
        f"FROM {_relation(table, schema, 'cur')}",
        f"WHERE {_flagged(claim.flag, 'cur')}",
        "  AND EXISTS (",
        "    SELECT 1",
        f"    FROM {_relation(table, schema, 'later')}",
        f"    WHERE {join[0]}",
        *(f"      AND {line}" for line in join[1:]),
        f'      AND "later".{m} > "cur".{m}',
        "  );",
    ]


def _determines(claim: Determines, relation: str) -> list[str]:
    """Render the values of the determinant seen with several dependents.

    Counted over distinct pairs rather than with `count(DISTINCT ...)`,
    which takes one column in most engines and a tuple in few.
    """
    both = (*claim.determinant, *claim.dependent)
    names = _names(claim.determinant)
    return [
        f'SELECT {names}, count(*) AS "dependents"',
        "FROM (",
        f"  SELECT DISTINCT {_names(both)}",
        f"  FROM {relation}",
        *(f"  {line}" for line in _present(both)),
        ') AS "pairs"',
        f"GROUP BY {names}",
        "HAVING count(*) > 1;",
    ]


def _missing(claim: Requires, relation: str) -> list[str]:
    """Render the rows holding what is given and lacking what it requires."""
    lacking = [f"{quoted(c.physical)} IS NULL" for c in claim.required]
    return [
        'SELECT count(*) AS "missing"',
        f"FROM {relation}",
        *_present(claim.given),
        f"  AND ({' OR '.join(lacking)});"
        if len(lacking) > 1
        else f"  AND {lacking[0]};",
    ]


def render(claim: Claim, schema: str = "mart", claimant: str = "") -> str:
    """Render one claim as a labelled query for the rows that break it.

    ``claimant`` names the extension that made the claim, shown on the
    label so a violation found by running the file can be traced to who
    asserted it. Empty for Varda's own, which every file is full of.
    """
    table = table_of(claim.subject)
    relation = _relation(table, schema)
    match claim:
        case PeriodNonEmpty():
            query = _empty_periods(claim, relation)
        case PeriodsDisjoint():
            query = _overlaps(claim, table, schema)
        case AtMostOneFlagged():
            query = _one_flagged(claim, relation)
        case FlaggedIsLatest():
            query = _latest(claim, table, schema)
        case Determines():
            query = _determines(claim, relation)
        case Requires():
            query = _missing(claim, relation)
        case _:
            assert_never(claim)
    by = f" (claimed by {claimant})" if claimant else ""
    label = f"{LABEL}{_where(claim.subject)}: {claim.statement}{by}"
    return "\n".join([label, *query])


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def collected(model: Model) -> tuple[tuple[str, Claim], ...]:
    """Gather every claim the active extensions make, with who made each.

    Varda's first, then each extension's in registry order, then ordered by
    the table each is about — the order the DDL creates them — so a table's
    claims read together whoever made them. A claim an extension hands over
    is checked before it is rendered, and one that cannot be is refused
    naming the extension, because a query against a column that is not
    there fails in the warehouse, long after the run that wrote it.
    """
    found: list[tuple[str, Claim]] = []
    for ext in model.registry.extensions:
        if ext.claims is None:
            continue
        try:
            made = tuple(ext.claims(model))
        except Exception as exc:
            # Deliberately broad, as for a rule: the job is to name whose
            # code failed rather than to show a traceback through it.
            msg = f"{ext.name}'s claims raised {type(exc).__name__}: {exc}"
            raise ClaimError(msg) from exc
        for claim in made:
            problem = claims.fault(claim, model)
            if problem is not None:
                msg = f"{ext.name} made a claim that {problem}: {claim!r}"
                raise ClaimError(msg)
            found.append(("" if ext.name == "varda" else ext.name, claim))
    order = {t.name: i for i, t in enumerate(analysis.creation_order(model))}
    found.sort(key=lambda pair: order[table_of(pair[1].subject).name])
    return tuple(found)


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
    blocks = [render(c, schema, who) for who, c in collected(model)]
    if not blocks:
        blocks = [NOTHING_CLAIMED]
    return "\n".join(header) + "\n\n".join(blocks) + "\n"


def run(ctx: Context) -> dict[str, str]:
    """Run this generator and return its artifacts."""
    return {ARTIFACT: generate(ctx.model, ctx.options.schema)}
