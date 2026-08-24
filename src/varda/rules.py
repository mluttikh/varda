"""Conformance rules.

Every rule answers one question: *is this a legal dimensional model?* Not *is
the data correct* — that is a data-quality concern this core does not address
— and not *is this a good model*, which is a review. The distinction matters
because these rules are meant to run in CI on every pull request, and a rule
that produces an argument gets switched off.

Rule numbering is stable and public, because "V203" is a thing an engineer can
search for, put in a commit message and grant an exemption against.
Renumbering a rule is a breaking change to the humans.

    V0xx  profile conformance — the annotations themselves
    V1xx  dimensional structure
    V2xx  measures
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from . import registry
from .anns import anns
from .ext import SEVERITIES, Severity
from .model import VERSIONING_ROLES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from .model import DimensionalModel, Level, Table


@dataclass(frozen=True)
class Finding:
    """One rule violation, against one named subject."""

    rule: str
    severity: Severity
    subject: str
    message: str

    def __str__(self) -> str:
        """Render as a severity line followed by an indented message."""
        mark = {"error": "ERROR", "warning": "WARN ", "info": "INFO "}[
            self.severity
        ]
        return f"{mark} {self.rule}  {self.subject}\n        {self.message}"


if TYPE_CHECKING:
    # Every rule has this shape, and saying so once is what lets the type
    # checker see through `@RULES.rule(...)` to the functions it decorates.
    # An untyped decorator silently erases the types of everything below it.
    RuleFn = Callable[[DimensionalModel], Iterator[Finding]]


@dataclass
class RuleSet:
    """A registry of rules, so ``varda rules`` can list them unrun.

    ``tag`` is the letters every code in this set begins with — ``V`` for
    Varda, ``ACME`` for an extension whose prefix is ``acme``. It is checked
    at registration rather than at load, because that is where the error can
    name the offending rule.
    """

    tag: str = ""
    rules: list[tuple[str, Severity, str, RuleFn]] = field(default_factory=list)

    def rule(
        self, code: str, severity: Severity, title: str
    ) -> Callable[[RuleFn], RuleFn]:
        """Register a rule function under a stable code and title."""
        self._check(code, severity)

        def decorate(fn: RuleFn) -> RuleFn:
            self.rules.append((code, severity, title, fn))
            return fn

        return decorate

    def _check(self, code: str, severity: Severity) -> None:
        """Reject a code this set may not mint, or one already taken.

        Both failures are unfixable once shipped rather than merely wrong. A
        code outside the set's tag collides with somebody else's namespace,
        and a rule code is a public identifier — it goes in commit messages
        and exemption lists, so two meanings for one code is a suppression
        that silently changes what it suppresses.
        """
        if severity not in SEVERITIES:
            msg = (
                f"{code}: severity {severity!r} is not one of "
                f"{', '.join(sorted(SEVERITIES))}"
            )
            raise ValueError(msg)
        pattern = rf"{re.escape(self.tag)}\d{{3}}"
        if self.tag and not re.fullmatch(pattern, code):
            msg = (
                f"{code} does not belong to rule set {self.tag!r}; "
                f"codes must look like {self.tag}001"
            )
            raise ValueError(msg)
        if any(code == existing for existing, *_ in self.rules):
            msg = f"{code} is already registered in rule set {self.tag!r}"
            raise ValueError(msg)


RULES = RuleSet(tag="V")

#: Below this, a grain is a phrase rather than a claim about a row. Crude on
#: purpose: it cannot tell a good grain from a bad one, and pretending
#: otherwise would make V104 an argument instead of a check.
GRAIN_MIN_WORDS = 4

#: One level is a column, not a path: nothing rolls up into anything.
HIERARCHY_MIN_LEVELS = 2


# ---------------------------------------------------------------------------
# V0xx — profile conformance
#
# These are the rules that make the extension mechanism safe. Without V001 a
# misspelled annotation is silently ignored, which means the difference
# between "this constraint is not enforced" and "this constraint is enforced"
# is a typo nobody can see.
# ---------------------------------------------------------------------------


def _annotated(
    model: DimensionalModel,
) -> Iterator[tuple[str, str, str, Any]]:
    """Walk every annotation in the model once.

    Yields ``(subject, target, tag, value)``. One shared walk rather than one
    per rule, because the three V0xx rules all need the same traversal and
    three copies of it is three places to forget a new kind of object.
    """
    for table in model.tables:
        for tag, value in anns(table.cls).items():
            yield str(table), "table", tag, value
        for column in table.columns:
            for tag, value in anns(column.slot).items():
                yield str(column), "column", tag, value


@RULES.rule("V001", "error", "Annotations are declared in a profile")
def v001(model: DimensionalModel) -> Iterator[Finding]:
    known = registry.prefixes()
    for subject, target, tag, _ in _annotated(model):
        prefix = tag.split(":", 1)[0] if ":" in tag else ""
        if prefix not in known:
            continue  # V003's business, not this rule's
        if tag in registry.declared_annotations(target):
            continue
        where = registry.profile_filename(prefix)
        yield Finding(
            "V001",
            "error",
            subject,
            f"unknown {target} annotation {tag!r}; "
            f"declare it in {where} or fix the typo",
        )


@RULES.rule("V002", "error", "Annotation values come from the declared enum")
def v002(model: DimensionalModel) -> Iterator[Finding]:
    for subject, target, tag, value in _annotated(model):
        enum = registry.annotation_enum(target, tag)
        if enum is None:
            continue
        allowed = registry.permitted(enum)
        if str(value) in allowed:
            continue
        yield Finding(
            "V002",
            "error",
            subject,
            f"{tag} is {str(value)!r}, which is not a value of {enum}; "
            f"expected one of {', '.join(allowed)}",
        )


@RULES.rule("V003", "warning", "Annotation prefixes belong to something")
def v003(model: DimensionalModel) -> Iterator[Finding]:
    """Flag an annotation whose prefix names no active extension.

    A warning rather than an error, and the distinction is the whole point of
    the rule. A model annotated by a team whose extension is not installed
    here is still a legal model — it is simply being read somewhere that
    cannot interpret part of it. Erroring would make models unportable; saying
    nothing would let a typo'd prefix hide a constraint that never applies.
    """
    known = registry.prefixes()
    declared = set(model.view.schema.prefixes or {})
    seen: set[str] = set()
    for subject, _, tag, _ in _annotated(model):
        if ":" not in tag:
            continue
        prefix = tag.split(":", 1)[0]
        if prefix in known or prefix in declared or prefix in seen:
            continue
        seen.add(prefix)
        yield Finding(
            "V003",
            "warning",
            subject,
            f"prefix {prefix!r} names no installed extension; "
            f"annotations under it are not being checked",
        )


# ---------------------------------------------------------------------------
# V1xx — dimensional structure
# ---------------------------------------------------------------------------


@RULES.rule("V101", "error", "Every table declares a role")
def v101(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        if table.role is None:
            yield Finding(
                "V101",
                "error",
                str(table),
                "no varda:role; every annotated class must declare "
                "FACT, DIMENSION or BRIDGE",
            )


@RULES.rule("V102", "error", "Every column declares a role")
def v102(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.columns:
            if column.role is None:
                yield Finding(
                    "V102",
                    "error",
                    str(column),
                    "no varda:role; a column with no declared role is one "
                    "no generator can place",
                )


@RULES.rule("V103", "error", "Every fact declares its grain")
def v103(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a fact whose row identity is undeclared.

    An error rather than a warning, on the same reasoning that makes
    additivity required: a fact whose grain is unknown is one whose measures
    cannot be safely aggregated through any join, so accepting it silently
    postpones the failure to whoever queries it.
    """
    for table in model.facts:
        if not table.grain:
            yield Finding(
                "V103",
                "error",
                str(table),
                "no varda:grain; name the columns at which rows are unique",
            )


@RULES.rule("V104", "warning", "Grain is stated as a sentence")
def v104(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a missing or too-short grain sentence.

    The length threshold is crude on purpose. It cannot tell a good sentence
    from a bad one, and pretending otherwise would make this an argument
    rather than a check. What it can catch is `grain_statement: daily` — a
    word where a sentence belongs, which is the form the failure almost
    always takes.
    """
    for table in model.facts:
        statement = (table.grain_statement or "").strip()
        if not statement:
            yield Finding(
                "V104",
                "warning",
                str(table),
                "no varda:grain_statement; say what one row is, "
                'conventionally "one row per ..."',
            )
        elif len(statement.split()) < GRAIN_MIN_WORDS:
            yield Finding(
                "V104",
                "warning",
                str(table),
                f"grain statement {statement!r} is a phrase, not a "
                f'sentence; conventionally "one row per ..."',
            )


@RULES.rule("V105", "error", "Every dimension has exactly one surrogate key")
def v105(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.dimensions:
        keys = table.surrogate_keys
        if len(keys) == 1:
            continue
        found = ", ".join(c.name for c in keys) or "none"
        yield Finding(
            "V105",
            "error",
            str(table),
            f"a dimension needs exactly one SURROGATE_KEY column; "
            f"found {len(keys)} ({found})",
        )


@RULES.rule("V106", "error", "Every dimension has a natural key")
def v106(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a dimension with no business identity.

    Without a natural key there is nothing for a loader to match on, so every
    load either creates duplicate rows or has the matching rule written
    somewhere the model cannot see.
    """
    for table in model.dimensions:
        if not table.natural_keys:
            yield Finding(
                "V106",
                "error",
                str(table),
                "no NATURAL_KEY column; nothing here says what makes two "
                "source rows the same business entity",
            )


@RULES.rule("V107", "error", "Every fact has at least one foreign key")
def v107(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.facts:
        if not table.foreign_keys:
            yield Finding(
                "V107",
                "error",
                str(table),
                "no FOREIGN_KEY column; a fact with no dimensions cannot "
                "be sliced by anything",
            )


@RULES.rule("V108", "error", "Every foreign key names its target")
def v108(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.foreign_keys:
            if not (column.references or "").strip():
                yield Finding(
                    "V108",
                    "error",
                    str(column),
                    "FOREIGN_KEY with no varda:references; name the class "
                    "it points at",
                )


@RULES.rule("V109", "error", "Foreign key targets exist")
def v109(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.foreign_keys:
            target = (column.references or "").strip()
            if not target or model.table(target) is not None:
                continue
            yield Finding(
                "V109",
                "error",
                str(column),
                f"references {target!r}, which is not a table in this model",
            )


@RULES.rule("V110", "error", "Foreign keys point at dimensions or bridges")
def v110(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a foreign key aimed at a fact.

    A fact referenced by another fact is the most common way a star quietly
    becomes a normalized schema: the join is now fact-to-fact, the grain of
    the result is neither table's, and no aggregate over it is safe.
    """
    for table in model.tables:
        for column in table.foreign_keys:
            target = model.table((column.references or "").strip())
            if target is None or not target.is_fact:
                continue
            yield Finding(
                "V110",
                "error",
                str(column),
                f"references {target.name!r}, which is a fact; foreign keys "
                f"point at dimensions or bridges",
            )


@RULES.rule("V111", "warning", "Every fact declares its temporal shape")
def v111(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.facts:
        if table.fact_type is None:
            yield Finding(
                "V111",
                "warning",
                str(table),
                "no varda:fact_type; loaders and generators cannot tell an "
                "insert-only table from one updated in place",
            )


@RULES.rule("V112", "error", "Roles sit on the right kind of table")
def v112(model: DimensionalModel) -> Iterator[Finding]:
    misplaced = {
        "SURROGATE_KEY": ("DIMENSION",),
        "NATURAL_KEY": ("DIMENSION",),
        "DEGENERATE_DIMENSION": ("FACT",),
        # Versioning columns describe a dimension row's history. A fact
        # records events that already happened and does not revise them, so
        # a version period on one is either a misunderstanding or an attempt
        # at bitemporality, which this core does not model.
        **dict.fromkeys(VERSIONING_ROLES, ("DIMENSION",)),
    }
    for table in model.tables:
        if table.role is None:
            continue  # V101 already said so
        for column in table.columns:
            allowed = misplaced.get(column.role or "")
            if allowed is None or table.role in allowed:
                continue
            yield Finding(
                "V112",
                "error",
                str(column),
                f"role {column.role!r} is only legal on a "
                f"{' or '.join(allowed)}, and {table.name} is a "
                f"{table.role}",
            )


@RULES.rule("V113", "warning", "Slowly-changing type is declared")
def v113(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a dimension that does not say what happens when a value changes.

    A warning rather than an error because a great many dimensions are
    genuinely type 1 and saying so feels like ceremony. It stays a rule
    because "we never decided" and "we decided overwrite" look identical in
    the model and cost very differently two years later.
    """
    for table in model.dimensions:
        if table.scd is None:
            yield Finding(
                "V113",
                "warning",
                str(table),
                "no varda:scd; nothing here says whether history is kept",
            )


@RULES.rule("V114", "error", "Grain columns are real and distinct")
def v114(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a grain naming a column the table lacks, or naming one twice.

    Both failures are silent by construction, in the same way V203's is. An
    unknown name is a claim about row identity that can never be checked
    against anything and looks exactly like one that can — and because the
    generator resolves the grain to columns it can find, the emitted
    constraint quietly covers *fewer* columns than were declared, which
    rejects legitimate rows and reads like broken source data.

    A repeated name is the same mistake wearing a different hat: it adds
    nothing to the constraint and means the modeler listed something twice
    without noticing.

    Checked on any table that declares a grain rather than on facts alone.
    Requiring one is a fact's business — V103 — but a grain that is wrong is
    wrong wherever it appears, and `examples/retail.yaml` puts one on a
    bridge.
    """
    for table in model.tables:
        have = {c.name for c in table.columns}
        seen: set[str] = set()
        for name in table.grain:
            if name not in have:
                yield Finding(
                    "V114",
                    "error",
                    str(table),
                    f"varda:grain names {name!r}, which is not a column "
                    f"of this table",
                )
            elif name in seen:
                yield Finding(
                    "V114",
                    "error",
                    str(table),
                    f"varda:grain names {name!r} twice; a column can only "
                    f"identify a row once",
                )
            seen.add(name)


@RULES.rule("V115", "error", "Grain columns locate a row")
def v115(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a grain built from columns that cannot identify a row.

    A grain is what a row *is*, not what it records. Only foreign keys and
    degenerate dimensions place a row in the model's dimensional space, so
    only those can compose a grain. A measure in a grain is the diagnostic
    form of a real confusion — a fact whose identity is defined by one of
    its own measurements is one where a second measurement of the same event
    silently becomes a second row.
    """
    allowed = {"FOREIGN_KEY", "DEGENERATE_DIMENSION"}
    for table in model.tables:
        for column in table.grain_columns:
            if column.role not in allowed:
                yield Finding(
                    "V115",
                    "error",
                    f"{table}.{column.name}",
                    f"role {column.role!r} cannot be part of a grain; "
                    f"a grain is composed of foreign keys and degenerate "
                    f"dimensions",
                )


@RULES.rule("V116", "error", "Versioning columns belong to a type-2 dimension")
def v116(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a version period on a dimension that keeps no versions.

    Type 0 retains the original value and type 1 overwrites it. Neither
    produces a second row, so a column bounding "this version" describes
    something the declared type says does not exist. One of the two is
    wrong, and which one is not for a validator to guess.
    """
    for table in model.dimensions:
        if table.scd is None or table.scd == "TYPE_2":
            continue  # V113 handles the missing case
        for column in table.versioning:
            yield Finding(
                "V116",
                "error",
                str(column),
                f"role {column.role!r} versions a row, but {table.name} is "
                f"{table.scd}, which keeps no versions",
            )


@RULES.rule("V117", "error", "A version period that ends also starts")
def v117(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a version end with no corresponding start.

    An end alone bounds nothing. The reverse is not a finding: storing only
    the start and deriving the end from the next version is a normal design,
    and Data Vault virtualizes the end column outright.
    """
    for table in model.dimensions:
        if table.version_ends and not table.version_starts:
            yield Finding(
                "V117",
                "error",
                str(table),
                "varda:role: VERSION_END with no VERSION_START; an end "
                "bounds nothing on its own",
            )


@RULES.rule("V118", "error", "At most one column per versioning role")
def v118(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a repeated versioning role on one table.

    Two starts is two answers to when a version began, and every consumer
    picks one — the generator by declaration order, the reader by whichever
    name looks more official. They will not always pick the same one.
    """
    for table in model.dimensions:
        seen: dict[str, list[str]] = {}
        for column in table.versioning:
            seen.setdefault(column.role or "", []).append(column.name)
        for role, names in sorted(seen.items()):
            if len(names) > 1:
                yield Finding(
                    "V118",
                    "error",
                    str(table),
                    f"{len(names)} columns claim role {role!r} "
                    f"({', '.join(sorted(names))}); at most one may",
                )


@RULES.rule("V119", "warning", "A type-2 dimension says how it versions")
def v119(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a type-2 dimension nothing can tell the versions of apart.

    Type 2 keeps a row per change, so something must distinguish those rows.
    Varda does not insist on a mechanism — the field uses a period, a flag
    and a counter, and calling any one mandatory would reject working
    designs. It insists that a *discriminator* be named, because a dimension
    versioning by a mechanism nobody declared cannot have its uniqueness
    generated or its current row found by anything but guesswork.

    A start instant and a counter discriminate; `IS_CURRENT` does not. It is
    true of exactly one version, so a key carrying it permits every superseded
    row to repeat — the constraint would be there and mean nothing. All three
    strategies Varda documents pair the flag with a start for this reason.
    """
    for table in model.dimensions:
        marks = (*table.version_starts, *table.version_numbers)
        if table.scd == "TYPE_2" and not marks:
            yield Finding(
                "V119",
                "warning",
                str(table),
                "TYPE_2 but no VERSION_START or VERSION_NUMBER; nothing "
                "tells one version from another, so no uniqueness can be "
                "generated (IS_CURRENT alone cannot: it marks one row)",
            )


@RULES.rule("V120", "error", "Table annotations sit on the right role")
def v120(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a table annotation on a kind of table it does not describe.

    `varda:fact_type` is the temporal shape of a fact; `varda:scd` is how a
    dimension answers a change. Neither means anything on the other kind of
    table, and neither is inert when misplaced: generators read both, so an
    `scd` on a fact emits DDL commented as keeping history the fact does not
    keep.

    V112 is this check for column roles. This is the same one a level up.
    """
    misplaced = {"fact_type": ("FACT",), "scd": ("DIMENSION",)}
    for table in model.tables:
        if table.role is None:
            continue  # V101 already said so
        for key, allowed in sorted(misplaced.items()):
            if getattr(table, key) is None or table.role in allowed:
                continue
            yield Finding(
                "V120",
                "error",
                str(table),
                f"varda:{key} describes a {' or '.join(allowed)}, and "
                f"{table.name} is a {table.role}",
            )


#: The column roles that cannot name a level to a reader.
#:
#: A level names what someone drilling the dimension sees, so this is a
#: question about display and not about identity. A surrogate key and a
#: foreign key are both fine identities and neither is readable — nobody
#: drills into `4718`. A measure is what gets aggregated rather than what it
#: is grouped by, and a versioning column separates versions of one row
#: rather than placing it in a hierarchy.
_NOT_A_LEVEL = (
    frozenset({"SURROGATE_KEY", "FOREIGN_KEY", "MEASURE"}) | VERSIONING_ROLES
)


@RULES.rule("V121", "error", "Hierarchy levels name real columns")
def v121(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a level naming a column that does not exist.

    The same check V114 makes of the grain, extended to the reference form.
    A level that names nothing is silently dropped by every generator, so the
    path a reader is offered is shorter than the one the model claims.

    ``country_key.country_name`` has three ways to be wrong and each gets its
    own message, because "not a column" would send a reader looking in the
    wrong table.
    """
    for table in model.tables:
        for hierarchy in table.hierarchies:
            for level in hierarchy.resolved:
                message = _level_fault(table, level)
                if message is None:
                    continue
                yield Finding("V121", "error", str(hierarchy), message)


def _level_fault(table: Table, level: Level) -> str | None:
    """Name what is wrong with one level, or ``None`` when it resolves."""
    if level.is_reference:
        return _reference_fault(table, level)
    if level.column is None:
        return f"level {level.spec!r} is not a column of {table.name}"
    return None


def _reference_fault(table: Table, level: Level) -> str | None:
    """Name what is wrong with a level reaching through a foreign key."""
    near, _, far = level.spec.partition(".")
    if level.via is None:
        return (
            f"level {level.spec!r} reaches through {near!r}, "
            f"which is not a column of {table.name}"
        )
    if level.via.role != "FOREIGN_KEY":
        return (
            f"level {level.spec!r} reaches through {near!r}, which is a "
            f"{level.via.role or 'column with no role'}; only a FOREIGN_KEY "
            "leads to another table"
        )
    if not level.via.references:
        return (
            f"level {level.spec!r} reaches through {near!r}, which names no "
            "target; see V108"
        )
    if level.column is None:
        return (
            f"level {level.spec!r} names {far!r}, which is not a column of "
            f"{level.via.references}"
        )
    return None


@RULES.rule("V122", "error", "Hierarchy levels are distinct")
def v122(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a column appearing twice in one hierarchy.

    A level that is its own ancestor is not a drill path. Usually a copy and
    paste, and always meaningless.
    """
    for table in model.tables:
        for hierarchy in table.hierarchies:
            seen: set[str] = set()
            for level in hierarchy.levels:
                if level in seen:
                    yield Finding(
                        "V122",
                        "error",
                        str(hierarchy),
                        f"level {level!r} appears more than once",
                    )
                seen.add(level)


@RULES.rule("V123", "error", "A hierarchy has at least two levels")
def v123(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a hierarchy of one level, or none.

    One level is a column, not a path. Nothing rolls up into anything, so
    every consumer that offers a drill-down offers a single step to nowhere.
    """
    for table in model.tables:
        for hierarchy in table.hierarchies:
            if len(hierarchy.levels) >= HIERARCHY_MIN_LEVELS:
                continue
            yield Finding(
                "V123",
                "error",
                str(hierarchy),
                f"{len(hierarchy.levels)} level(s); a hierarchy needs at "
                "least two, coarsest first",
            )


@RULES.rule("V124", "error", "Hierarchy names are unique within a table")
def v124(model: DimensionalModel) -> Iterator[Finding]:
    """Flag two hierarchies on one table sharing a name.

    A dimension carrying several paths is the normal case — a date dimension
    has a calendar path and a week path — and the name is the only thing
    telling a reader which one they are drilling. Two of them answering to
    the same name makes the choice unresolvable.
    """
    for table in model.tables:
        seen = set()
        for hierarchy in table.hierarchies:
            if not hierarchy.name:
                yield Finding(
                    "V124",
                    "error",
                    str(table),
                    "a hierarchy with no name; every path needs one",
                )
                continue
            if hierarchy.name in seen:
                yield Finding(
                    "V124",
                    "error",
                    str(table),
                    f"two hierarchies named {hierarchy.name!r}",
                )
            seen.add(hierarchy.name)


@RULES.rule(
    "V125", "error", "Hierarchy levels are the kind of column a level can be"
)
def v125(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a level named by a column a reader cannot drill.

    A bare foreign key gets its own message, because it is the near-miss a
    snowflake invites: the coarser levels are their own tables, so the only
    thing to hand is the key, and a path of keys renders as integers. The
    reference form reaches past it to something readable.
    """
    for table in model.tables:
        for hierarchy in table.hierarchies:
            for level in hierarchy.resolved:
                column = level.column
                if column is None or column.role not in _NOT_A_LEVEL:
                    continue
                if column.role == "FOREIGN_KEY" and not level.is_reference:
                    target = column.references or "the dimension it points at"
                    message = (
                        f"level {level.spec!r} is a foreign key, which reads "
                        f"as an integer; name what it points at instead — "
                        f"{level.spec}.<column of {target}>"
                    )
                else:
                    message = (
                        f"level {level.spec!r} is named by a {column.role}, "
                        "which a reader cannot drill; a level is an "
                        "ATTRIBUTE or a NATURAL_KEY"
                    )
                yield Finding("V125", "error", str(hierarchy), message)


#: The column roles that cannot identify a level's members. A measure is a
#: quantity rather than an identity, and a versioning column separates
#: versions of one member rather than one member from another. Every other
#: role identifies something: keys obviously, and an attribute whenever the
#: modeller says it does.
_NOT_A_KEY = frozenset({"MEASURE"}) | VERSIONING_ROLES


@RULES.rule("V127", "error", "A declared level key identifies")
def v127(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a level key naming a column that cannot identify one.

    The key answers "which member", where the level's column answers "what
    is it called". A key column that does not exist identifies nothing, and
    one holding a measure or a version marker identifies the wrong thing.

    Whether the columns are jointly unique is not checked, for the same
    reason the grain sentence is not: it is a claim about data.
    """
    for table in model.tables:
        for hierarchy in table.hierarchies:
            for level in hierarchy.resolved:
                name = level.declared_key
                if not name:
                    continue
                column = table.column(name)
                if column is None:
                    yield Finding(
                        "V127",
                        "error",
                        str(hierarchy),
                        f"level {level.spec!r} is keyed on {name!r}, "
                        f"which is not a column of {table.name}",
                    )
                elif column.role in _NOT_A_KEY:
                    yield Finding(
                        "V127",
                        "error",
                        str(hierarchy),
                        f"level {level.spec!r} is keyed on {name!r}, "
                        f"which is a {column.role} and identifies no member",
                    )


@RULES.rule("V126", "error", "Hierarchies belong to dimensions")
def v126(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a hierarchy on a fact or a bridge.

    A drill path describes descriptive context, which is what a dimension
    is. A fact is drilled *through* its dimensions, and a bridge exists to
    resolve a many-to-many rather than to be navigated.

    V120 is this check for the other table annotations.
    """
    for table in model.tables:
        if table.role is None or table.is_dimension:
            continue  # V101 reports a missing role
        if not table.hierarchies:
            continue
        yield Finding(
            "V126",
            "error",
            str(table),
            f"varda:hierarchies describes a DIMENSION, and {table.name} "
            f"is a {table.role}",
        )


@RULES.rule("V128", "error", "Unique keys name real columns")
def v128(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a ``unique_keys`` entry naming a column the table does not have.

    LinkML accepts one without complaint — a key over a misspelled slot
    loads clean and constrains nothing — so this is the only thing standing
    between a declared uniqueness claim and no constraint at all.
    """
    for table in model.tables:
        for unique in table.unique_keys:
            have = {c.name for c in unique.columns}
            for name in unique.declared:
                if name in have:
                    continue
                yield Finding(
                    "V128",
                    "error",
                    str(unique),
                    f"unique key names {name!r}, which is not a column of "
                    f"{table.name}",
                )


@RULES.rule("V129", "error", "A type-2 business key includes its version")
def v129(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a business unique key on a type-2 dimension with no version.

    A type-2 dimension keeps a row per change, so its business key repeats
    once per version. A unique key over business columns alone says it does
    not, which is false about the table and would reject the second version
    of every row.

    Only keys carrying a natural key are checked: one over the surrogate key
    is unique already and needs nothing added.
    """
    for table in model.dimensions:
        if table.scd != "TYPE_2":
            continue
        versions = (*table.version_starts, *table.version_numbers)
        marks = {c.name for c in versions}
        for unique in table.unique_keys:
            names = {c.name for c in unique.columns}
            if not names & {c.name for c in table.natural_keys}:
                continue
            if names & marks:
                continue
            yield Finding(
                "V129",
                "error",
                str(unique),
                "a business key on a type-2 dimension repeats once per "
                "version; add the column that marks versions apart",
            )


@RULES.rule("V130", "warning", "Natural keys are covered by a unique key")
def v130(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a natural key no declared unique key covers.

    Only where a table declares its own ``unique_keys``. Without them Varda
    derives the constraint from the natural key itself and the question does
    not arise; with them the derived one is not emitted, so a natural key
    named in none of them is a business identity nothing enforces.
    """
    for table in model.dimensions:
        if not table.unique_keys:
            continue
        covered = {c.name for u in table.unique_keys for c in u.columns}
        for column in table.natural_keys:
            if column.name in covered:
                continue
            yield Finding(
                "V130",
                "warning",
                str(column),
                "a natural key in none of this table's unique keys; the "
                "identity a loader matches on is not enforced",
            )


# ---------------------------------------------------------------------------
# V2xx — measures
#
# This family exists because the wrong answer here is the most expensive
# error a dimensional model produces. A structural mistake usually breaks a
# query; an additivity mistake returns a number that looks entirely
# reasonable and is wrong, to someone who will act on it.
# ---------------------------------------------------------------------------


@RULES.rule("V201", "error", "Every measure declares its additivity")
def v201(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.measures:
            if column.additivity is None:
                yield Finding(
                    "V201",
                    "error",
                    str(column),
                    "no varda:additivity; a measure nobody has classified "
                    "will be summed by someone",
                )


@RULES.rule("V202", "error", "Semi-additive measures name their exception")
def v202(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.measures:
            if column.additivity != "SEMI_ADDITIVE":
                continue
            if (column.semi_additive_over or "").strip():
                continue
            yield Finding(
                "V202",
                "error",
                str(column),
                "SEMI_ADDITIVE with no varda:semi_additive_over; name the "
                "foreign key it may not be summed across",
            )


@RULES.rule("V203", "error", "The semi-additive exception is a real key")
def v203(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a semi-additive exception naming a column the fact does not have.

    The failure this catches is silent by construction: a constraint that
    names a non-existent dimension is a constraint that never fires, and it
    looks exactly like one that does.
    """
    for table in model.tables:
        keys = {c.name for c in table.foreign_keys}
        for column in table.measures:
            over = (column.semi_additive_over or "").strip()
            if not over or over in keys:
                continue
            found = ", ".join(sorted(keys)) or "none"
            yield Finding(
                "V203",
                "error",
                str(column),
                f"semi_additive_over names {over!r}, which is not a foreign "
                f"key of {table.name} (has: {found})",
            )


@RULES.rule("V204", "error", "Measures live on facts and bridges")
def v204(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a measure on a dimension.

    A numeric column on a dimension is usually an attribute — a size, a band,
    a credit limit. When it genuinely is a measure, the dimension is doing a
    fact's job and the grain of any aggregate over it is undefined.
    """
    for table in model.dimensions:
        for column in table.measures:
            yield Finding(
                "V204",
                "error",
                str(column),
                "a measure on a dimension; give it role ATTRIBUTE, or move "
                "it to a fact at the grain it is measured at",
            )


@RULES.rule("V205", "warning", "Every measure declares its unit")
def v205(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a measure with no unit.

    Units are LinkML's own ``unit``, so this rule reads a native rather than
    an annotation. A ``unit`` naming the measure in none of the ways a reader
    would recognize counts as undeclared.
    """
    for table in model.tables:
        for column in table.measures:
            if not (column.unit or "").strip():
                yield Finding(
                    "V205",
                    "warning",
                    str(column),
                    "no unit; two measures in different currencies "
                    "add up cleanly and wrongly",
                )


@RULES.rule("V206", "warning", "A fact carries measures, or says it does not")
def v206(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.facts:
        if table.measures or table.fact_type == "FACTLESS":
            continue
        yield Finding(
            "V206",
            "warning",
            str(table),
            "no measures; if that is deliberate, declare "
            "varda:fact_type: FACTLESS",
        )


# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------


def all_rules() -> list[tuple[str, Severity, str, RuleFn]]:
    """Every registered rule, from every active extension, sorted by code.

    Sorted by code alone rather than by extension, because a reader looking
    for V203 wants it where V202 was, and an operator reading a run wants the
    same ordering every time regardless of what is installed.
    """
    out: list[tuple[str, Severity, str, RuleFn]] = []
    for extension in registry.extensions():
        if extension.rules is not None:
            out.extend(extension.rules.rules)
    return sorted(out, key=lambda r: r[0])


def check(
    model: DimensionalModel, exemptions: Iterable[str] | None = None
) -> list[Finding]:
    """Run every rule and return the findings, in rule order.

    ``exemptions`` names rules to skip entirely. Severity overrides come from
    the registry, which has already refused any that two extensions disagree
    about.
    """
    skip = set(exemptions or ())
    overrides = registry.severities()
    out: list[Finding] = []
    for code, _, _, fn in all_rules():
        if code in skip:
            continue
        out.extend(_at_severity(f, overrides) for f in fn(model))
    return out


def _at_severity(finding: Finding, overrides: dict[str, Severity]) -> Finding:
    """Apply a configured severity override to one finding."""
    wanted = overrides.get(finding.rule)
    if wanted is None or wanted == finding.severity:
        return finding
    return replace(finding, severity=wanted)


def unknown_codes(names: Iterable[str]) -> list[str]:
    """Name the given codes that no active extension registers.

    Used to catch an exemption that has outlived the rule it suppressed —
    a suppression nobody notices has stopped applying.
    """
    known = {code for code, *_ in all_rules()}
    return sorted(set(names) - known)
