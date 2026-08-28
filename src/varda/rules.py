"""Conformance rules.

Every rule answers one question: *is this a legal dimensional model?* Not *is
the data correct* — that is a data-quality concern this core does not address
— and not *is this a good model*, which is a review. The distinction matters
because these rules are meant to run in CI on every pull request, and a rule
that produces an argument gets switched off.

A code is public — "V703" is a thing an engineer searches for, puts in a
commit message and grants an exemption against — so renumbering one is a
breaking change to the humans, and it is a change 0.x is still allowed to
make. At 1.0 the numbers freeze; until then a band is worth more than the
stability of any code inside it.

    V0xx  the annotations themselves
    V1xx  roles, and where each one is legal
    V2xx  grain
    V3xx  identity
    V4xx  references between tables
    V5xx  time
    V6xx  hierarchies
    V7xx  measures
    V8xx  physical naming and types

A code names its concern, so `varda rules` comes out grouped and a new rule
has an obvious home. That is the whole reason the bands exist: `V1xx` had
grown to thirty-four rules across ten unrelated topics, and a family that
covers everything tells a reader nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from . import registry
from .anns import anns, get
from .ext import SEVERITIES, Severity
from .model import (
    FACET_MINIMUM,
    FACETS,
    ONE_IDENTITY,
    VERSIONING_ROLES,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from .model import Column, DimensionalModel, Level, Table


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
#: otherwise would make V202 an argument instead of a check.
GRAIN_MIN_WORDS = 4

#: One level is a column, not a path: nothing rolls up into anything.
HIERARCHY_MIN_LEVELS = 2

#: Two claims on one physical name is the smallest collision there is.
COLLIDING = 2


# -------------------------------------------------------------------------
# V0xx — the annotations themselves
#
# These are the rules that make the extension mechanism safe. Without
# V001 a misspelled annotation is silently ignored, which means the
# difference between "this constraint is not enforced" and "this
# constraint is enforced" is a typo nobody can see.
# -------------------------------------------------------------------------


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


@RULES.rule("V004", "error", "Structured annotations use declared fields")
def v004(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a field name inside a structured annotation that is not declared.

    V001 catches a misspelled annotation *name*, and stops there. One level
    down the same typo is just as silent and reads worse: `levles:` for
    `levels:` was reported as a hierarchy with zero levels, and `colunm:` for
    `column:` as ``level '' is not a column``. Neither message names the
    mistake, and both describe a model nobody wrote.

    Checked against the profile that declares the range, so an extension
    introducing its own structured annotation is checked on the same terms as
    `varda:hierarchies`.
    """
    for subject, target, tag, value in _annotated(model):
        shape = registry.annotation_shape(target, tag)
        if shape is None:
            continue  # a scalar or an enum; V002's business if anything
        prefix = tag.split(":", 1)[0]
        for path, unknown, expected in _stray_fields(value, shape, prefix):
            yield Finding(
                "V004",
                "error",
                subject,
                f"{tag}{path} has no field {unknown!r}; "
                f"expected one of {', '.join(expected)}",
            )


def _stray_fields(
    value: Any, shape: dict[str, str], prefix: str, path: str = ""
) -> Iterator[tuple[str, str, list[str]]]:
    """Walk a structured value, yielding every field its range does not name.

    Recurses wherever a declared field's own range is structured, which is
    how a typo inside a hierarchy's `levels` is reached.
    """
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue  # a bare level is a string, and legal
        for key, nested in entry.items():
            name = str(key)
            if name not in shape:
                yield path, name, sorted(shape)
                continue
            deeper = registry.structured_shape(prefix, shape[name])
            if deeper:
                yield from _stray_fields(
                    nested, deeper, prefix, f"{path}.{name}"
                )


# -------------------------------------------------------------------------
# V1xx — roles, and where each one is legal
#
# What a table is, what a column is, and the placements that make no
# sense. Every other family reads the roles these establish.
# -------------------------------------------------------------------------


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


@RULES.rule("V103", "error", "Roles sit on the right kind of table")
def v103(model: DimensionalModel) -> Iterator[Finding]:
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
                "V103",
                "error",
                str(column),
                f"role {column.role!r} is only legal on a "
                f"{' or '.join(allowed)}, and {table.name} is a "
                f"{table.role}",
            )


@RULES.rule("V104", "error", "Table annotations sit on the right role")
def v104(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a table annotation on a kind of table it does not describe.

    `varda:fact_type` is the temporal shape of a fact; `varda:scd` is how a
    dimension answers a change. Neither means anything on the other kind of
    table, and neither is inert when misplaced: generators read both, so an
    `scd` on a fact emits DDL commented as keeping history the fact does not
    keep.

    V103 is this check for column roles. This is the same one a level up.
    """
    misplaced = {"fact_type": ("FACT",), "scd": ("DIMENSION",)}
    for table in model.tables:
        if table.role is None:
            continue  # V101 already said so
        for key, allowed in sorted(misplaced.items()):
            if getattr(table, key) is None or table.role in allowed:
                continue
            yield Finding(
                "V104",
                "error",
                str(table),
                f"varda:{key} describes a {' or '.join(allowed)}, and "
                f"{table.name} is a {table.role}",
            )


# -------------------------------------------------------------------------
# V2xx — grain
#
# What one row of a table is. Declared twice, as a column set a
# validator can test and a sentence it cannot.
# -------------------------------------------------------------------------


@RULES.rule("V201", "error", "Every fact declares its grain")
def v201(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a fact whose row identity is undeclared.

    An error rather than a warning, on the same reasoning that makes
    additivity required: a fact whose grain is unknown is one whose measures
    cannot be safely aggregated through any join, so accepting it silently
    postpones the failure to whoever queries it.
    """
    for table in model.facts:
        if not table.grain:
            yield Finding(
                "V201",
                "error",
                str(table),
                "no varda:grain; name the columns at which rows are unique",
            )


@RULES.rule("V202", "warning", "Grain is stated as a sentence")
def v202(model: DimensionalModel) -> Iterator[Finding]:
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
                "V202",
                "warning",
                str(table),
                "no varda:grain_statement; say what one row is, "
                'conventionally "one row per ..."',
            )
        elif len(statement.split()) < GRAIN_MIN_WORDS:
            yield Finding(
                "V202",
                "warning",
                str(table),
                f"grain statement {statement!r} is a phrase, not a "
                f'sentence; conventionally "one row per ..."',
            )


@RULES.rule("V203", "error", "Grain columns are real and distinct")
def v203(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a grain naming a column the table lacks, or naming one twice.

    Both failures are silent by construction, in the same way V703's is. An
    unknown name is a claim about row identity that can never be checked
    against anything and looks exactly like one that can — and because the
    generator resolves the grain to columns it can find, the emitted
    constraint quietly covers *fewer* columns than were declared, which
    rejects legitimate rows and reads like broken source data.

    A repeated name is the same mistake wearing a different hat: it adds
    nothing to the constraint and means the modeler listed something twice
    without noticing.

    Checked on any table that declares a grain rather than on facts alone.
    Requiring one is a fact's business — V201 — but a grain that is wrong is
    wrong wherever it appears, and `examples/retail.yaml` puts one on a
    bridge.
    """
    for table in model.tables:
        have = {c.name for c in table.columns}
        seen: set[str] = set()
        for name in table.grain:
            if name not in have:
                yield Finding(
                    "V203",
                    "error",
                    str(table),
                    f"varda:grain names {name!r}, which is not a column "
                    f"of this table",
                )
            elif name in seen:
                yield Finding(
                    "V203",
                    "error",
                    str(table),
                    f"varda:grain names {name!r} twice; a column can only "
                    f"identify a row once",
                )
            seen.add(name)


@RULES.rule("V204", "error", "Grain columns locate a row")
def v204(model: DimensionalModel) -> Iterator[Finding]:
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
                    "V204",
                    "error",
                    f"{table}.{column.name}",
                    f"role {column.role!r} cannot be part of a grain; "
                    f"a grain is composed of foreign keys and degenerate "
                    f"dimensions",
                )


# -------------------------------------------------------------------------
# V3xx — identity
#
# What makes two rows the same thing: the surrogate key facts join to,
# the natural key a loader matches on, and the uniqueness a model
# declares for itself.
# -------------------------------------------------------------------------


@RULES.rule("V301", "error", "Every dimension has exactly one surrogate key")
def v301(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.dimensions:
        keys = table.surrogate_keys
        if len(keys) == 1:
            continue
        found = ", ".join(c.name for c in keys) or "none"
        yield Finding(
            "V301",
            "error",
            str(table),
            f"a dimension needs exactly one SURROGATE_KEY column; "
            f"found {len(keys)} ({found})",
        )


@RULES.rule("V302", "error", "Every dimension has a natural key")
def v302(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a dimension with no business identity.

    Without a natural key there is nothing for a loader to match on, so every
    load either creates duplicate rows or has the matching rule written
    somewhere the model cannot see.
    """
    for table in model.dimensions:
        if not table.natural_keys:
            yield Finding(
                "V302",
                "error",
                str(table),
                "no NATURAL_KEY column; nothing here says what makes two "
                "source rows the same business entity",
            )


@RULES.rule("V303", "error", "Unique keys name real columns")
def v303(model: DimensionalModel) -> Iterator[Finding]:
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
                    "V303",
                    "error",
                    str(unique),
                    f"unique key names {name!r}, which is not a column of "
                    f"{table.name}",
                )


@RULES.rule("V304", "error", "A type-2 business key includes its version")
def v304(model: DimensionalModel) -> Iterator[Finding]:
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
                "V304",
                "error",
                str(unique),
                "a business key on a type-2 dimension repeats once per "
                "version; add the column that marks versions apart",
            )


@RULES.rule("V305", "warning", "Natural keys are covered by a unique key")
def v305(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a natural key no declared unique key covers.

    Only where a table declares its own ``unique_keys``, because a declared
    key replaces the derived one: a natural key named in none of them is a
    business identity nothing enforces. Where nothing is declared, one
    natural key derives its own constraint and several are V306's, which is
    an error rather than this warning — there the answer is not missing, it
    is unknowable from the roles.
    """
    for table in model.dimensions:
        if not table.unique_keys:
            continue
        covered = {c.name for u in table.unique_keys for c in u.columns}
        for column in table.natural_keys:
            if column.name in covered:
                continue
            yield Finding(
                "V305",
                "warning",
                str(column),
                "a natural key in none of this table's unique keys; the "
                "identity a loader matches on is not enforced",
            )


@RULES.rule(
    "V306", "error", "A dimension with several natural keys declares them"
)
def v306(model: DimensionalModel) -> Iterator[Finding]:
    """Flag several natural keys with no ``unique_keys`` to disambiguate them.

    Two columns marked NATURAL_KEY mean one of two things, and a role cannot
    say which. Either they are one compound identity — a store known by its
    chain code and its store number — or they are two alternative ones, a
    product carrying a barcode from one source and a supplier's part number
    from the other. The two want opposite constraints: `UNIQUE (a, b)` for
    the first, `UNIQUE (a)` and `UNIQUE (b)` for the second.

    Varda used to pick the first, silently, for both. On a table that meant
    the second that constraint is weaker than either key alone — two rows may
    share a barcode as long as their part numbers differ — and a NULL on
    either side leaves the row unconstrained altogether, which is the normal
    state of a table loaded from two sources that each fill one column. The
    model passed `--strict` with nothing to say.

    An error rather than a warning because both silent outcomes are wrong:
    the merged constraint enforces something nobody meant, and deriving
    nothing leaves a dimension whose identity the database does not hold.
    The model has to say which, and `unique_keys` is where LinkML already
    says it.
    """
    for table in model.dimensions:
        if table.unique_keys:
            continue  # said, and V304 and V305 check what was said
        keys = table.natural_keys
        if len(keys) <= ONE_IDENTITY:
            continue
        named = ", ".join(c.name for c in keys)
        yield Finding(
            "V306",
            "error",
            str(table),
            f"{len(keys)} natural keys ({named}) and no unique_keys; one "
            f"compound identity and several alternative ones need different "
            f"constraints, and a role cannot tell them apart",
        )


@RULES.rule("V307", "warning", "A lone natural key is required")
def v307(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a dimension whose only business identity may be absent.

    SQL counts NULLs as distinct, so `UNIQUE (gtin)` over a nullable column
    admits any number of rows that have no gtin at all — the identity is
    enforced for every row except the ones that do not have it, which are the
    rows a duplicate load produces.

    Only where there is one natural key, and that scoping is the rule. Where
    a dimension has several, being absent is usually the point: a product
    read from a barcode feed has no supplier part number and one read from a
    supplier catalog has no barcode, and each row fills the column its source
    knows. Warning there would fire on every well-formed table of that shape
    and be switched off, taking this with it.
    """
    for table in model.dimensions:
        keys = table.natural_keys
        if len(keys) != ONE_IDENTITY:
            continue
        if keys[0].required:
            continue
        yield Finding(
            "V307",
            "warning",
            str(keys[0]),
            "the only natural key here, and not required; SQL counts NULLs "
            "as distinct, so the uniqueness over it does not hold for a row "
            "that has none",
        )


# -------------------------------------------------------------------------
# V4xx — references between tables
#
# Foreign keys and what they may point at. A star becomes a normalized
# schema one wrong reference at a time.
# -------------------------------------------------------------------------


@RULES.rule("V401", "error", "Every fact has at least one foreign key")
def v401(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.facts:
        if not table.foreign_keys:
            yield Finding(
                "V401",
                "error",
                str(table),
                "no FOREIGN_KEY column; a fact with no dimensions cannot "
                "be sliced by anything",
            )


@RULES.rule("V402", "error", "Every foreign key names its target")
def v402(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.foreign_keys:
            if not (column.references or "").strip():
                yield Finding(
                    "V402",
                    "error",
                    str(column),
                    "FOREIGN_KEY with no varda:references; name the class "
                    "it points at",
                )


@RULES.rule("V403", "error", "Foreign key targets exist")
def v403(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.foreign_keys:
            target = (column.references or "").strip()
            if not target or model.table(target) is not None:
                continue
            yield Finding(
                "V403",
                "error",
                str(column),
                f"references {target!r}, which is not a table in this model",
            )


@RULES.rule("V404", "error", "Foreign keys point at dimensions or bridges")
def v404(model: DimensionalModel) -> Iterator[Finding]:
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
                "V404",
                "error",
                str(column),
                f"references {target.name!r}, which is a fact; foreign keys "
                f"point at dimensions or bridges",
            )


@RULES.rule("V405", "error", "A bridge references something")
def v405(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a bridge with no foreign key.

    A bridge exists to resolve a many-to-many. One referencing nothing
    relates nothing, and V401 makes the same demand of a fact for the same
    reason: a table whose whole purpose is to connect others must name at
    least one.

    One rather than two, deliberately. The obvious reading of a bridge is
    two keys and a weight, but Kimball's group-key form carries only one —
    the fact points at a group, and the bridge maps that group to a
    dimension — so requiring a pair would refuse a standard design.
    """
    for table in model.bridges:
        if table.foreign_keys:
            continue
        yield Finding(
            "V405",
            "error",
            str(table),
            "no FOREIGN_KEY column; a bridge resolves a many-to-many, and "
            "one that references nothing resolves nothing",
        )


# -------------------------------------------------------------------------
# V5xx — time
#
# How a table behaves as the data behind it changes: a fact's temporal
# shape, and a dimension's answer to a source that has been updated.
# -------------------------------------------------------------------------


@RULES.rule("V501", "warning", "Every fact declares its temporal shape")
def v501(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.facts:
        if table.fact_type is None:
            yield Finding(
                "V501",
                "warning",
                str(table),
                "no varda:fact_type; loaders and generators cannot tell an "
                "insert-only table from one updated in place",
            )


@RULES.rule("V502", "warning", "Slowly-changing type is declared")
def v502(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a dimension that does not say what happens when a value changes.

    A warning rather than an error because a great many dimensions are
    genuinely type 1 and saying so feels like ceremony. It stays a rule
    because "we never decided" and "we decided overwrite" look identical in
    the model and cost very differently two years later.
    """
    for table in model.dimensions:
        if table.scd is None:
            yield Finding(
                "V502",
                "warning",
                str(table),
                "no varda:scd; nothing here says whether history is kept",
            )


@RULES.rule("V503", "error", "Versioning columns belong to a type-2 dimension")
def v503(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a version period on a dimension that keeps no versions.

    Type 0 retains the original value and type 1 overwrites it. Neither
    produces a second row, so a column bounding "this version" describes
    something the declared type says does not exist. One of the two is
    wrong, and which one is not for a validator to guess.
    """
    for table in model.dimensions:
        if table.scd is None or table.scd == "TYPE_2":
            continue  # V502 handles the missing case
        for column in table.versioning:
            yield Finding(
                "V503",
                "error",
                str(column),
                f"role {column.role!r} versions a row, but {table.name} is "
                f"{table.scd}, which keeps no versions",
            )


@RULES.rule("V504", "error", "A version period that ends also starts")
def v504(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a version end with no corresponding start.

    An end alone bounds nothing. The reverse is not a finding: storing only
    the start and deriving the end from the next version is a normal design,
    and Data Vault virtualizes the end column outright.
    """
    for table in model.dimensions:
        if table.version_ends and not table.version_starts:
            yield Finding(
                "V504",
                "error",
                str(table),
                "varda:role: VERSION_END with no VERSION_START; an end "
                "bounds nothing on its own",
            )


@RULES.rule("V505", "error", "At most one column per versioning role")
def v505(model: DimensionalModel) -> Iterator[Finding]:
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
                    "V505",
                    "error",
                    str(table),
                    f"{len(names)} columns claim role {role!r} "
                    f"({', '.join(sorted(names))}); at most one may",
                )


@RULES.rule("V506", "warning", "A type-2 dimension says how it versions")
def v506(model: DimensionalModel) -> Iterator[Finding]:
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
                "V506",
                "warning",
                str(table),
                "TYPE_2 but no VERSION_START or VERSION_NUMBER; nothing "
                "tells one version from another, so no uniqueness can be "
                "generated (IS_CURRENT alone cannot: it marks one row)",
            )


# -------------------------------------------------------------------------
# V6xx — hierarchies
#
# The named paths a dimension is drilled down. The largest family, and
# the one whose claims are least checkable: that levels are real is a
# question about the schema, and that each rolls up into exactly one
# parent is a question about the data.
# -------------------------------------------------------------------------


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


@RULES.rule("V601", "error", "Hierarchy levels name real columns")
def v601(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a level naming a column that does not exist.

    The same check V203 makes of the grain, extended to the reference form.
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
                yield Finding("V601", "error", str(hierarchy), message)


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
            "target; see V402"
        )
    if level.column is None:
        return (
            f"level {level.spec!r} names {far!r}, which is not a column of "
            f"{level.via.references}"
        )
    return None


@RULES.rule("V602", "error", "Hierarchy levels are distinct")
def v602(model: DimensionalModel) -> Iterator[Finding]:
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
                        "V602",
                        "error",
                        str(hierarchy),
                        f"level {level!r} appears more than once",
                    )
                seen.add(level)


@RULES.rule("V603", "error", "A hierarchy has at least two levels")
def v603(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a hierarchy of one level, or none.

    One level is a column, not a path. Nothing rolls up into anything, so
    every consumer that offers a drill-down offers a single step to nowhere.
    """
    for table in model.tables:
        for hierarchy in table.hierarchies:
            if len(hierarchy.levels) >= HIERARCHY_MIN_LEVELS:
                continue
            yield Finding(
                "V603",
                "error",
                str(hierarchy),
                f"{len(hierarchy.levels)} level(s); a hierarchy needs at "
                "least two, coarsest first",
            )


@RULES.rule("V604", "error", "Hierarchy names are unique within a table")
def v604(model: DimensionalModel) -> Iterator[Finding]:
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
                    "V604",
                    "error",
                    str(table),
                    "a hierarchy with no name; every path needs one",
                )
                continue
            if hierarchy.name in seen:
                yield Finding(
                    "V604",
                    "error",
                    str(table),
                    f"two hierarchies named {hierarchy.name!r}",
                )
            seen.add(hierarchy.name)


@RULES.rule(
    "V605", "error", "Hierarchy levels are the kind of column a level can be"
)
def v605(model: DimensionalModel) -> Iterator[Finding]:
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
                yield Finding("V605", "error", str(hierarchy), message)


@RULES.rule("V606", "error", "Hierarchies belong to dimensions")
def v606(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a hierarchy on a fact or a bridge.

    A drill path describes descriptive context, which is what a dimension
    is. A fact is drilled *through* its dimensions, and a bridge exists to
    resolve a many-to-many rather than to be navigated.

    V104 is this check for the other table annotations.
    """
    for table in model.tables:
        if table.role is None or table.is_dimension:
            continue  # V101 reports a missing role
        if not table.hierarchies:
            continue
        yield Finding(
            "V606",
            "error",
            str(table),
            f"varda:hierarchies describes a DIMENSION, and {table.name} "
            f"is a {table.role}",
        )


#: The column roles that cannot identify a level's members. A measure is a
#: quantity rather than an identity, and a versioning column separates
#: versions of one member rather than one member from another. Every other
#: role identifies something: keys obviously, and an attribute whenever the
#: modeler says it does.
_NOT_A_KEY = frozenset({"MEASURE"}) | VERSIONING_ROLES


def _key_owner(table: Table, level: Level) -> str:
    """Name the table a level's declared key must be a column of.

    The one the level's column came from. `column` and `key` describe one
    level, so a level written `country_key.country_name` is named and
    identified by DimCountry, and reporting DimCity would send a reader to
    look for the key where it was never meant to be.
    """
    if level.is_reference and level.via is not None and level.via.references:
        return level.via.references
    return table.name


@RULES.rule("V607", "error", "A declared level key identifies")
def v607(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a level key naming a column that cannot identify one.

    The key answers "which member", where the level's column answers "what
    is it called". A key column that does not exist identifies nothing, and
    one holding a measure or a version marker identifies the wrong thing.

    A key is looked for in whichever table supplied the level's column, so a
    level reached through a foreign key is keyed in the dimension it names
    rather than in the near table.

    Whether the columns are jointly unique is not checked, for the same
    reason the grain sentence is not: it is a claim about data.
    """
    for table in model.tables:
        for hierarchy in table.hierarchies:
            for level in hierarchy.resolved:
                name = level.declared_key
                if not name:
                    continue
                column = level.key
                if column is None:
                    yield Finding(
                        "V607",
                        "error",
                        str(hierarchy),
                        f"level {level.spec!r} is keyed on {name!r}, "
                        f"which is not a column of {_key_owner(table, level)}",
                    )
                elif column.role in _NOT_A_KEY:
                    yield Finding(
                        "V607",
                        "error",
                        str(hierarchy),
                        f"level {level.spec!r} is keyed on {name!r}, "
                        f"which is a {column.role} and identifies no member",
                    )


# -------------------------------------------------------------------------
# V7xx — measures
#
# This family exists because the wrong answer here is the most
# expensive error a dimensional model produces. A structural mistake
# usually breaks a query; an additivity mistake returns a number that
# looks entirely reasonable and is wrong, to someone who will act on
# it.
# -------------------------------------------------------------------------


@RULES.rule("V701", "error", "Every measure declares its additivity")
def v701(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.measures:
            if column.additivity is None:
                yield Finding(
                    "V701",
                    "error",
                    str(column),
                    "no varda:additivity; a measure nobody has classified "
                    "will be summed by someone",
                )


@RULES.rule("V702", "error", "Semi-additive measures name their exception")
def v702(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.measures:
            if column.additivity != "SEMI_ADDITIVE":
                continue
            if (column.semi_additive_over or "").strip():
                continue
            yield Finding(
                "V702",
                "error",
                str(column),
                "SEMI_ADDITIVE with no varda:semi_additive_over; name the "
                "foreign key it may not be summed across",
            )


@RULES.rule("V703", "error", "The semi-additive exception is a real key")
def v703(model: DimensionalModel) -> Iterator[Finding]:
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
                "V703",
                "error",
                str(column),
                f"semi_additive_over names {over!r}, which is not a foreign "
                f"key of {table.name} (has: {found})",
            )


@RULES.rule("V704", "error", "Measures live on facts and bridges")
def v704(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a measure on a dimension.

    A numeric column on a dimension is usually an attribute — a size, a band,
    a credit limit. When it genuinely is a measure, the dimension is doing a
    fact's job and the grain of any aggregate over it is undefined.
    """
    for table in model.dimensions:
        for column in table.measures:
            yield Finding(
                "V704",
                "error",
                str(column),
                "a measure on a dimension; give it role ATTRIBUTE, or move "
                "it to a fact at the grain it is measured at",
            )


@RULES.rule("V705", "warning", "Every measure declares its unit")
def v705(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a measure with no unit.

    Units are LinkML's own ``unit``, so this rule reads a native rather than
    an annotation. A ``unit`` naming the measure in none of the ways a reader
    would recognize counts as undeclared.
    """
    for table in model.tables:
        for column in table.measures:
            if not (column.unit or "").strip():
                yield Finding(
                    "V705",
                    "warning",
                    str(column),
                    "no unit; two measures in different currencies "
                    "add up cleanly and wrongly",
                )


@RULES.rule("V706", "warning", "A fact carries measures, or says it does not")
def v706(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.facts:
        if table.measures or table.fact_type == "FACTLESS":
            continue
        yield Finding(
            "V706",
            "warning",
            str(table),
            "no measures; if that is deliberate, declare "
            "varda:fact_type: FACTLESS",
        )


@RULES.rule("V707", "warning", "Decimal measures declare precision and scale")
def v707(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a decimal measure that does not say what it keeps.

    A warning, and scoped to measures on purpose. Every string column in a
    model could be asked for a width on the same reasoning, and asking would
    fire a dozen times on a small star and be switched off. This fires rarely
    and covers the case that costs money: a measure is the number somebody
    acts on, and a decimal one silently rounded is wrong in the way that
    still looks like an answer.
    """
    for table in model.tables:
        for column in table.measures:
            if not column.parameterizes("precision"):
                continue
            missing = [
                f"varda:{name}"
                for name in ("precision", "scale")
                if get(column.slot, name) is None
            ]
            if not missing:
                continue
            yield Finding(
                "V707",
                "warning",
                str(column),
                f"no {' or '.join(missing)}; a bare NUMERIC is unconstrained "
                f"on PostgreSQL and DECIMAL(18, 3) on DuckDB, so what this "
                f"measure keeps depends on where it lands",
            )


# -------------------------------------------------------------------------
# V8xx — physical naming and types
#
# What the generators emit. A physical name identifies one table or one
# column, or it identifies nothing; a type facet parameterizes the type
# emitted beside it, or it parameterizes nothing.
# -------------------------------------------------------------------------


def _claim(names: list[str]) -> str:
    """Phrase the colliding names, saying when they differ only in case.

    Quoting an identifier does not settle case everywhere. PostgreSQL treats
    `"Foo"` and `"foo"` as two tables; DuckDB refuses the second as a
    duplicate. A model is checked once and may be generated for any of the
    dialects, so a pair that only some databases distinguish is refused here
    rather than left to whichever one the output meets first.
    """
    unique = sorted(set(names))
    if len(unique) == 1:
        return f"physical name {unique[0]!r} is"
    listed = " and ".join(repr(n) for n in unique)
    return (
        f"physical names {listed} differ only in case, which not every "
        f"database distinguishes, and are"
    )


def _named_by(obj: Any, logical: str) -> str:
    """Say where an object's physical name came from.

    The realistic collision is not two identical `varda:physical_name`
    declarations — it is one declared name meeting one derived from a class
    or slot name. A message that only says "duplicate" sends a reader looking
    for an annotation that is not there.
    """
    if get(obj, "physical_name"):
        return f"{logical} (varda:physical_name)"
    return f"{logical} (derived)"


@RULES.rule("V801", "error", "Physical table names are unique")
def v801(model: DimensionalModel) -> Iterator[Finding]:
    """Flag two classes that emit one table.

    Everything is generated into one schema, so a physical name identifies a
    table or it identifies nothing. Two classes sharing one emit two
    `CREATE TABLE` statements for the same name: the database refuses the
    second, and the model quietly describes a table the warehouse does not
    have.

    It happens without anybody writing the same name twice. `DimCustomer`
    and `Dim_Customer` both derive `dim_customer`.
    """
    claimed: dict[str, list[Table]] = {}
    for table in model.tables:
        claimed.setdefault(table.physical.casefold(), []).append(table)
    for _, tables in sorted(claimed.items()):
        if len(tables) < COLLIDING:
            continue
        who = " and ".join(_named_by(t.cls, t.name) for t in tables)
        what = _claim([t.physical for t in tables])
        yield Finding(
            "V801",
            "error",
            str(tables[0]),
            f"{what} claimed by {who}; one name cannot be two tables",
        )


@RULES.rule("V802", "error", "Physical column names are unique in a table")
def v802(model: DimensionalModel) -> Iterator[Finding]:
    """Flag two columns of one table that emit one column.

    The same mistake a level down, and the same silence: the emitted
    `CREATE TABLE` carries the name twice and no database accepts it.

    Checked over the induced columns, so a slot inherited from a parent
    collides with a slot declared here exactly as it would if both were
    written in the same class.
    """
    for table in model.tables:
        claimed: dict[str, list[Column]] = {}
        for column in table.columns:
            claimed.setdefault(column.physical.casefold(), []).append(column)
        for _, columns in sorted(claimed.items()):
            if len(columns) < COLLIDING:
                continue
            who = " and ".join(_named_by(c.slot, c.name) for c in columns)
            what = _claim([c.physical for c in columns])
            yield Finding(
                "V802",
                "error",
                str(table),
                f"{what} claimed by columns {who}; "
                f"one name cannot be two columns",
            )


@RULES.rule("V803", "error", "Type facets are well formed")
def v803(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a type facet that cannot be emitted as written.

    A facet parameterizes the SQL type — the 80 in `VARCHAR(80)`, the 18 and
    the 2 in `NUMERIC(18, 2)` — so it belongs in the physical band with the
    names, and it fails in the same silent way they do. `varda:max_length`
    on a date parameterizes nothing and is dropped; a scale with no precision
    has no DDL to become. Both leave a model that reads as though it stated
    a width and generates output that never had one.
    """
    for table in model.tables:
        for column in table.columns:
            yield from _facet_faults(column)


def _facet_faults(column: Column) -> Iterator[Finding]:
    """Report every way one column's facets fail to describe a type."""
    declared = {
        name: value
        for name in FACETS
        if (value := get(column.slot, name)) is not None
    }
    for name, value in declared.items():
        yield from _one_facet(column, name, value)

    if "scale" in declared and "precision" not in declared:
        yield Finding(
            "V803",
            "error",
            str(column),
            "varda:scale with no varda:precision; there is no NUMERIC(, 2) "
            "to emit, and the missing half would have to be invented",
        )
        return

    precision, scale = column.precision, column.scale
    if precision is not None and scale is not None and scale > precision:
        yield Finding(
            "V803",
            "error",
            str(column),
            f"varda:scale {scale} is larger than varda:precision "
            f"{precision}; a column cannot keep more digits after the "
            f"decimal point than it keeps in total",
        )


def _one_facet(column: Column, name: str, value: str) -> Iterator[Finding]:
    """Check one facet against the range it parameterizes and its own bound.

    Legality is the column's to answer, because it depends on the whole type
    chain rather than on the name the slot happens to mention — see
    :meth:`varda.model.Column.parameterizes`.
    """
    if not column.parameterizes(name):
        legal = ", ".join(sorted(FACETS[name]))
        resolved = " -> ".join(column.type_chain)
        where = (
            f"a {column.range} column"
            if len(column.type_chain) == 1
            else f"a {column.range} column ({resolved})"
        )
        yield Finding(
            "V803",
            "error",
            str(column),
            f"varda:{name} on {where}, where it "
            f"parameterizes nothing; legal on: {legal}",
        )
        return
    if column.facet(name) is None:
        least = FACET_MINIMUM[name]
        yield Finding(
            "V803",
            "error",
            str(column),
            f"varda:{name} is {value!r}; expected a whole number "
            f"of {least} or more",
        )


@RULES.rule("V804", "error", "Every range names a type the schema knows")
def v804(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a range that is not a type this schema can resolve.

    LinkML does not object to a range naming nothing, and neither did
    anything here: `range: intger` and `range: uuid` both passed `varda
    check` with no findings and then stopped `varda generate` with a
    GenerationError. A model that validates and cannot be generated from is
    the worst of the two answers, because the first one is the one people
    trust.

    Checked against the schema's own types rather than against the SQL
    generator's table, and the difference is deliberate. `curie` is a real
    LinkML type that Varda has no column type for; that is one generator's
    limit, reported by that generator, and an extension emitting something
    other than DDL may well handle it. A range naming nothing is wrong for
    everybody.
    """
    view = model.view
    types = set(view.all_types())
    classes = set(view.all_classes())
    enums = set(view.all_enums())
    for table in model.tables:
        for column in table.columns:
            rng = column.range
            if rng in types:
                continue
            yield Finding(
                "V804",
                "error",
                str(column),
                f"range {rng!r} {_range_is(rng, classes, enums)}",
            )


def _range_is(rng: str, classes: set[str], enums: set[str]) -> str:
    """Say what a range turned out to be, and what to do about it."""
    if rng in classes:
        return (
            "names a class, not a type; a column pointing at another table "
            "is a FOREIGN_KEY whose target is varda:references"
        )
    if rng in enums:
        return (
            "names an enum; Varda maps types to columns and has no column "
            "type for a permissible-value set"
        )
    return (
        "names no type in this schema. `uuid` is Varda's and needs "
        "`imports: - varda`; anything else is a typo or a type to declare"
    )


# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------


def all_rules() -> list[tuple[str, Severity, str, RuleFn]]:
    """Every registered rule, from every active extension, sorted by code.

    Sorted by code alone rather than by extension, because a reader looking
    for V703 wants it where V702 was, and an operator reading a run wants the
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
