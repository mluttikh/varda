"""Claims — what a model says about its data, as data.

A rule asks whether a model is legal. A claim is what a legal model says
about the rows it will hold, and that no constraint can state: that one
column set determines another, that the versions of one entity are each in
force for a while and never at once. A generator renders a claim; Varda's
renders each as a query in `sql/expectations.sql` returning the rows that
break it, and another could render the same claims as a dbt test or a
data-quality suite without deciding again what the model meant.

The shapes are closed, as the vocabulary is. An extension contributes
claims through :attr:`varda.ext.Extension.claims` and builds them from these
types; it does not add a shape, because every renderer dispatches
exhaustively on them, and a shape only one renderer knows is a claim the
others silently skip.

Every claim is about one table — the one its ``subject`` belongs to — and
every column it names is a column of that table. A claim spanning two
tables is a join, and a join is where a check starts to encode a model of
its own.

``statement`` is the claim as a sentence, the one a violation report
shows. Varda's own name the columns as the SQL quotes them, so a report can
be read against the query beside it.

:func:`implied` is what Varda concludes a model claims, and is public on the
same weaker promise as :mod:`varda.analysis`: an answer may become stricter
in a minor release while its signature holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import analysis
from .gen_sql import quoted
from .snapshot import VERSIONING_ROLES, SlowlyChangingType, table_of

if TYPE_CHECKING:
    from .snapshot import Column, Element, Hierarchy, Level, Model, Table


@dataclass(frozen=True, eq=False)
class Determines:
    """Rows agreeing on ``determinant`` agree on ``dependent``.

    A functional dependency, checked over the rows holding every column of
    both: a null on either side is not part of the claim.
    """

    subject: Element
    statement: str
    determinant: tuple[Column, ...]
    dependent: tuple[Column, ...]


@dataclass(frozen=True, eq=False)
class PeriodNonEmpty:
    """Every row's period holds at least one instant.

    The period is closed at ``start`` and open at ``end``, as the profile
    defines a version's, so it is empty when ``end`` is not after ``start``.
    A null ``end`` is open and never empty.
    """

    subject: Element
    statement: str
    start: Column
    end: Column


@dataclass(frozen=True, eq=False)
class PeriodsDisjoint:
    """No two rows sharing ``key`` have periods that overlap.

    Closed at ``start``, open at ``end``, and a null ``end`` open for good,
    so two periods meeting at a boundary do not overlap. Two starting at
    the same instant are left to a unique key, which says so more plainly.
    """

    subject: Element
    statement: str
    key: tuple[Column, ...]
    start: Column
    end: Column


@dataclass(frozen=True, eq=False)
class AtMostOneFlagged:
    """No two rows sharing ``key`` both have ``flag`` set."""

    subject: Element
    statement: str
    key: tuple[Column, ...]
    flag: Column


@dataclass(frozen=True, eq=False)
class FlaggedIsLatest:
    """No flagged row has a later row, by ``order``, sharing its ``key``."""

    subject: Element
    statement: str
    key: tuple[Column, ...]
    flag: Column
    order: Column


@dataclass(frozen=True, eq=False)
class Requires:
    """Every row holding each column of ``given`` holds each of ``required``.

    What a `NOT NULL` would say, for a column the model leaves nullable on
    purpose: a level of a drill path is absent for nobody, though the
    column holding it may be absent in a row that holds no level below it.
    """

    subject: Element
    statement: str
    given: tuple[Column, ...]
    required: tuple[Column, ...]


#: Every shape a claim may take. A renderer matches on this and ends in
#: `assert_never`, so a shape added here is a type error until handled.
Claim = (
    Determines
    | PeriodNonEmpty
    | PeriodsDisjoint
    | AtMostOneFlagged
    | FlaggedIsLatest
    | Requires
)


def columns(claim: Claim) -> tuple[Column, ...]:
    """Every column a claim names, in the order its fields name them."""
    match claim:
        case Determines():
            return (*claim.determinant, *claim.dependent)
        case PeriodNonEmpty():
            return (claim.start, claim.end)
        case PeriodsDisjoint():
            return (*claim.key, claim.start, claim.end)
        case AtMostOneFlagged():
            return (*claim.key, claim.flag)
        case FlaggedIsLatest():
            return (*claim.key, claim.flag, claim.order)
        case Requires():
            return (*claim.given, *claim.required)


def fault(claim: Claim, model: Model) -> str | None:
    """Say why a claim cannot be rendered against ``model``, if it cannot.

    Asked of every claim an extension hands over, because a claim naming a
    column of another table, or a table of another model, renders as a
    query against something that is not there.
    """
    table = table_of(claim.subject)
    if model.table(table.name) is not table:
        return f"is about {table.name}, which is not a table of this model"
    if not claim.statement.strip():
        return "has no statement"
    named = columns(claim)
    stray = [c for c in named if c.table is not table]
    if stray:
        return (
            f"names {stray[0]}, which is not a column of {table.name}; a "
            "claim is about one table"
        )
    return _empty(claim)


def _empty(claim: Claim) -> str | None:
    """Say which of a claim's column sets is empty, if one is."""
    match claim:
        case Determines() if not claim.determinant or not claim.dependent:
            return "determines nothing, or by nothing"
        case Requires() if not claim.given or not claim.required:
            return "requires nothing, or of nothing"
        case PeriodsDisjoint() | AtMostOneFlagged() | FlaggedIsLatest() if (
            not claim.key
        ):
            return "has no key"
        case _:
            return None


# ---------------------------------------------------------------------------
# What Varda concludes a model claims
# ---------------------------------------------------------------------------


def _shown(columns: tuple[Column, ...]) -> str:
    """Render columns the way a statement names them."""
    return f"({', '.join(quoted(c.physical) for c in columns)})"


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


def _history(table: Table) -> list[Claim]:
    """Every claim a type-2 dimension's versioning columns make.

    Only what the model claims. The current row is claimed only where a
    flag says which one it is: a window closed by a far-future sentinel has
    no current row that could be found without guessing the sentinel, and
    two open versions overlap, which the window's own claim catches.
    """
    if table.scd != SlowlyChangingType.TYPE_2:
        return []
    out: list[Claim] = []
    start = table.version_starts[0] if table.version_starts else None
    end = table.version_ends[0] if table.version_ends else None
    if start is not None and end is not None:
        out.append(
            PeriodNonEmpty(
                table,
                "every version is in force for a while "
                f"({quoted(start.physical)} < {quoted(end.physical)})",
                start,
                end,
            )
        )
    marks = table.version_starts or table.version_numbers
    for key in business_keys(table):
        if start is not None and end is not None:
            out.append(
                PeriodsDisjoint(
                    table,
                    f"versions of {_shown(key)} do not overlap",
                    key,
                    start,
                    end,
                )
            )
        if not table.current_flags:
            continue
        flag = table.current_flags[0]
        out.append(
            AtMostOneFlagged(
                table,
                f"at most one version of {_shown(key)} is current",
                key,
                flag,
            )
        )
        if marks:
            out.append(
                FlaggedIsLatest(
                    table,
                    f"the current version of {_shown(key)} is its latest",
                    key,
                    flag,
                    marks[0],
                )
            )
    return out


def _roll_up(hierarchy: Hierarchy, i: int) -> Claim | None:
    """Claim one level whose key identifies on its own rolls up, or nothing.

    A foreign key names one row of another dimension, so it is a whole
    identity on its own and can sit under one parent only. A plain level's
    column identifies a member only under its parent — several states have
    a Springfield — so it claims this only where the hierarchy is declared
    strict. A level reached through a foreign key that declares its own key
    is identified in the table it reaches, which this table's rows cannot
    see, and is left alone.
    """
    table = hierarchy.table
    level = hierarchy.levels[i]
    key = analysis.level_key(level)
    if key is None or key.table is not table:
        return None
    if level.is_reference:
        via = level.via
        if level.key is not None or via is None:
            return None
        if analysis.reference(via) is None:
            return None  # V402, V403 or V406 says why
    elif not hierarchy.strict:
        return None
    parent = analysis.identities(hierarchy)[i - 1]
    if not parent or any(c.table is not table for c in parent):
        return None
    above = hierarchy.levels[i - 1]
    return Determines(
        hierarchy,
        f"each {level.spec} is in one {above.spec}",
        (key,),
        parent,
    )


def _named_once(hierarchy: Hierarchy, i: int) -> Claim | None:
    """Claim one level whose key is not its name is named one way, or nothing.

    The key says which member a row belongs to, and the column what that
    member is called; one member with two names is two members that the key
    cannot tell apart.
    """
    table = hierarchy.table
    level = hierarchy.levels[i]
    column = level.column
    key = analysis.level_key(level)
    if level.is_reference or level.key is None or column is None:
        return None
    if key is None or key is column:
        return None
    identity = analysis.identities(hierarchy)[i]
    if any(c.table is not table for c in (*identity, column)):
        return None
    return Determines(
        hierarchy,
        f"each {_shown(identity)} is named one {quoted(column.physical)}",
        identity,
        (column,),
    )


def _here(level: Level) -> Column | None:
    """Name the column of the hierarchy's own table that holds a level.

    The level's own column for a plain level, and the foreign key for one
    reached through it: the row holds the key, and the name is the other
    table's.
    """
    return level.via if level.is_reference else level.column


def _covered(hierarchy: Hierarchy, i: int) -> Claim | None:
    """Claim a row holding one level holds the level above, or nothing.

    Nothing where the column above is required, which the DDL states
    already, and where both levels are held by one column — two levels
    reached through one foreign key are present together or not at all.
    """
    table = hierarchy.table
    child, parent = _here(hierarchy.levels[i]), _here(hierarchy.levels[i - 1])
    if child is None or parent is None or child is parent:
        return None
    if child.table is not table or parent.table is not table:
        return None
    if parent.required:
        return None
    level, above = hierarchy.levels[i], hierarchy.levels[i - 1]
    return Requires(
        hierarchy,
        f"every {level.spec} has a {above.spec}",
        (child,),
        (parent,),
    )


def _hierarchies(table: Table) -> list[Claim]:
    """Every claim a table's hierarchies make that rows can show.

    Completeness for every dimension: a version with a brand and no
    category is as broken as a row. Roll-up and naming not for a type-2
    dimension. Its rows are versions, and a city moving region is history
    rather than a contradiction; checking each version alone needs the
    current row, which only some designs mark.
    """
    versioned = table.scd == SlowlyChangingType.TYPE_2
    out: list[Claim] = []
    for hierarchy in table.hierarchies:
        for i in range(len(hierarchy.levels)):
            found: list[Claim | None] = []
            if i > 0 and hierarchy.complete:
                found.append(_covered(hierarchy, i))
            if not versioned:
                found.append(_roll_up(hierarchy, i) if i > 0 else None)
                found.append(_named_once(hierarchy, i))
            out += [c for c in found if c is not None]
    return out


def implied(model: Model) -> tuple[Claim, ...]:
    """Every claim Varda reads a model as making, table by table.

    In creation order, so the queries read in the order the DDL creates the
    tables they are about; within a table, its history before its
    hierarchies.
    """
    return tuple(
        claim
        for table in analysis.creation_order(model)
        for claim in (*_history(table), *_hierarchies(table))
    )
