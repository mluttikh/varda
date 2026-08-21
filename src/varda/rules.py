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

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from .model import DimensionalModel


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
                "no varda:role; every annotated class must be a fact, "
                "a dimension or a bridge",
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
    for table in model.facts:
        if not (table.grain or "").strip():
            yield Finding(
                "V103",
                "error",
                str(table),
                "no varda:grain; state what exactly one row represents",
            )


@RULES.rule("V104", "warning", "Grain is stated as a sentence")
def v104(model: DimensionalModel) -> Iterator[Finding]:
    """Flag a grain too short to be a claim about a row.

    The threshold is crude on purpose. It cannot tell a good grain from a bad
    one, and pretending otherwise would make this an argument rather than a
    check. What it can catch is `grain: daily` — a word where a sentence
    belongs, which is the form the failure almost always takes.
    """
    for table in model.facts:
        grain = (table.grain or "").strip()
        if grain and len(grain.split()) < GRAIN_MIN_WORDS:
            yield Finding(
                "V104",
                "warning",
                str(table),
                f"grain {grain!r} is a phrase, not a sentence; "
                f'conventionally "one row per ..."',
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
            f"a dimension needs exactly one surrogate_key column; "
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
                "no natural_key column; nothing here says what makes two "
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
                "no foreign_key column; a fact with no dimensions cannot "
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
                    "foreign_key with no varda:references; name the class "
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
    becomes a normalised schema: the join is now fact-to-fact, the grain of
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
        "surrogate_key": ("dimension",),
        "natural_key": ("dimension",),
        "degenerate_dimension": ("fact",),
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
            if column.additivity != "semi_additive":
                continue
            if (column.semi_additive_over or "").strip():
                continue
            yield Finding(
                "V202",
                "error",
                str(column),
                "semi_additive with no varda:semi_additive_over; say which "
                "dimension it may not be summed across",
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
                "measure on a dimension; make it an attribute, or move it "
                "to a fact at the grain it is measured at",
            )


@RULES.rule("V205", "warning", "Every measure declares its unit")
def v205(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.tables:
        for column in table.measures:
            if not (column.unit or "").strip():
                yield Finding(
                    "V205",
                    "warning",
                    str(column),
                    "no varda:unit; two measures in different currencies "
                    "add up cleanly and wrongly",
                )


@RULES.rule("V206", "warning", "A fact carries measures, or says it does not")
def v206(model: DimensionalModel) -> Iterator[Finding]:
    for table in model.facts:
        if table.measures or table.fact_type == "factless":
            continue
        yield Finding(
            "V206",
            "warning",
            str(table),
            "no measures; if that is deliberate, declare "
            "varda:fact_type: factless",
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
