"""The extension interface — the module a third party imports.

An extension is one party's contribution to Varda: a namespace prefix, a
LinkML profile declaring the annotations that prefix permits, and optionally
a set of conformance rules and generators. Varda itself is an instance of
:class:`Extension` (see :mod:`varda.registry`), which is the test that this
interface is real rather than a second-class bolt-on: if the core can be
expressed through it, a third party is not working around anything.

Everything an extension runs on is reachable from here, types included: the
model a rule walks (:class:`Model` and the elements under it), the
:mod:`~varda.analysis` Varda reads into it, the :class:`Finding` a rule
yields and the :class:`RuleSet` it registers in, and the :class:`Context` a
generator is given. An extension imports this module and nothing else.

The governing principle is **one party, one namespace; extensions add, they
never redefine**. An extension may introduce annotations, enumerations and
rules under its own prefix. It may not add a value to ``TableRole``, change
what ``semi_additive`` means, or replace a Varda generator — because every
generator dispatches exhaustively on those closed vocabularies and raises on
a value it cannot map. Widening one from outside turns that discipline into a
fallback path. See the note above ``enums:`` in ``profile/varda.yaml``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import analysis
from .snapshot import (
    Additivity,
    Annotations,
    Column,
    ColumnRole,
    Element,
    FactType,
    Hierarchy,
    Level,
    Model,
    Ref,
    SlowlyChangingType,
    Table,
    TableRole,
    UniqueKey,
)
from .vocabulary import Vocabulary

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Iterable, Mapping

__all__ = [
    "Additivity",
    "Annotations",
    "Column",
    "ColumnRole",
    "Context",
    "Element",
    "Extension",
    "ExtensionError",
    "FactType",
    "Finding",
    "Generator",
    "Hierarchy",
    "Issue",
    "Level",
    "Model",
    "Options",
    "Ref",
    "Report",
    "Rule",
    "RuleError",
    "RuleSet",
    "Setting",
    "Severity",
    "SlowlyChangingType",
    "Table",
    "TableRole",
    "UniqueKey",
    "Vocabulary",
    "analysis",
]

#: A prefix is a namespace, and it becomes a YAML key, a Python identifier and
#: part of a filename. The intersection of what all three accept is narrow.
PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: A rule tag is the letters every code in a set begins with: `V` for Varda,
#: `ACME` for an extension whose prefix is `acme`.
TAG_PATTERN = re.compile(r"^[A-Z]+$")


class Severity(StrEnum):
    """How much a rule's finding matters to the run that found it."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @classmethod
    def parse(cls, value: str, where: str, code: str = "") -> Severity:
        """Read a severity someone wrote, or raise naming where they wrote it.

        An unrecognized severity used to reach the renderer, which indexes a
        closed table, so a one-character typo ended the run in a `KeyError`
        rather than a sentence naming the file.
        """
        try:
            return cls(value)
        except ValueError:
            known = ", ".join(sorted(s.value for s in cls))
            what = f" for {code}" if code else ""
            msg = f"{where}: severity {value!r}{what} is not one of {known}"
            raise ExtensionError(msg) from None


class ExtensionError(Exception):
    """An extension is malformed, or collides with another one.

    Always raised, never warned. The cost of raising is a clear error at
    startup; the cost of warning is an estate that half-works for two years
    because nobody reads startup warnings.
    """


class RuleError(Exception):
    """A rule raised, and the run stopped rather than reporting less.

    A rule that cannot run is a check that is not happening, and `varda
    check` reporting the other rules and exiting zero would be the tool
    saying a model conforms when part of the question was never asked.
    """


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """What a rule says: this element, and what is wrong with it.

    Which rule said it, at what severity and about which file is the
    engine's to add — every rule would otherwise state the code and severity
    its decorator already holds, and sixty rules answering one question are
    sixty chances to answer it differently.
    """

    subject: Element
    message: str


@dataclass(frozen=True)
class Rule:
    """One conformance rule, under a stable code."""

    code: str
    severity: Severity
    title: str
    check: Callable[[Model], Iterable[Finding]]

    @property
    def doc(self) -> str | None:
        """The rule's reasoning, where its title does not say enough."""
        return self.check.__doc__


class RuleSet:
    """A registry of rules, so ``varda rules`` can list them unrun.

    ``tag`` is the letters every code in this set begins with — ``V`` for
    Varda, ``ACME`` for an extension whose prefix is ``acme``. It is checked
    at registration rather than at load, because that is where the error can
    name the offending rule.
    """

    def __init__(self, tag: str = "") -> None:
        """Start an empty set whose codes all begin with ``tag``."""
        self.tag = tag
        self._rules: list[Rule] = []

    @property
    def rules(self) -> tuple[Rule, ...]:
        """Every rule registered here, in registration order."""
        return tuple(self._rules)

    def rule(
        self, code: str, severity: Severity | str, title: str
    ) -> Callable[
        [Callable[[Model], Iterable[Finding]]],
        Callable[[Model], Iterable[Finding]],
    ]:
        """Register a rule function under a stable code and title."""
        level = self._check(code, severity)

        def decorate(
            fn: Callable[[Model], Iterable[Finding]],
        ) -> Callable[[Model], Iterable[Finding]]:
            self._rules.append(Rule(code, level, title, fn))
            return fn

        return decorate

    def _check(self, code: str, severity: Severity | str) -> Severity:
        """Reject a code this set may not mint, or one already taken.

        Both failures are unfixable once shipped rather than merely wrong. A
        code outside the set's tag collides with somebody else's namespace,
        and a rule code is a public identifier — it goes in commit messages
        and exemption lists, so two meanings for one code is a suppression
        that silently changes what it suppresses.
        """
        try:
            level = Severity(severity)
        except ValueError:
            known = ", ".join(sorted(s.value for s in Severity))
            msg = f"{code}: severity {severity!r} is not one of {known}"
            raise ValueError(msg) from None
        pattern = rf"{re.escape(self.tag)}\d{{3}}"
        if self.tag and not re.fullmatch(pattern, code):
            msg = (
                f"{code} does not belong to rule set {self.tag!r}; "
                f"codes must look like {self.tag}001"
            )
            raise ValueError(msg)
        if any(code == existing.code for existing in self._rules):
            msg = f"{code} is already registered in rule set {self.tag!r}"
            raise ValueError(msg)
        return level


@dataclass(frozen=True)
class Issue:
    """One finding, as the engine reports it.

    The rule's finding plus what only the engine can say: which rule, at
    what severity once the repository's overrides apply, and which file the
    subject was declared in when that is not the file the run was pointed
    at.
    """

    rule: Rule
    severity: Severity
    subject: Element
    message: str
    #: Empty unless the subject's table arrived through ``imports:``, so the
    #: common case — a model in one file — reads exactly as it always did.
    origin: str = ""

    @property
    def code(self) -> str:
        """The rule's code."""
        return self.rule.code

    def __str__(self) -> str:
        """Render as a severity line followed by an indented message."""
        mark = {
            Severity.ERROR: "ERROR",
            Severity.WARNING: "WARN ",
            Severity.INFO: "INFO ",
        }[self.severity]
        where = f"  (imported from {self.origin})" if self.origin else ""
        return (
            f"{mark} {self.code}  {self.subject}{where}\n        {self.message}"
        )


@dataclass(frozen=True)
class Report:
    """The outcome of checking one model."""

    model: Model
    issues: tuple[Issue, ...]
    rules_run: int
    #: Configured exemptions naming no registered rule — a suppression
    #: nobody has noticed has stopped applying.
    stale_exemptions: tuple[str, ...] = ()
    #: Configured severity overrides naming no registered rule.
    stale_overrides: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[Issue, ...]:
        """The issues at error severity, after overrides."""
        return self._at(Severity.ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        """The issues at warning severity, after overrides."""
        return self._at(Severity.WARNING)

    @property
    def stale(self) -> bool:
        """Flag whether any configured code names no registered rule."""
        return bool(self.stale_exemptions or self.stale_overrides)

    def _at(self, severity: Severity) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == severity)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """How a run asks for its artifacts, as opposed to what they are of.

    ``dialect`` and ``constraints`` are names rather than types, so that
    this module stays free of SQL: the SQL generator owns the dialects and
    the levels and looks these up, an extension's own SQL generator may know
    engines the core does not, and a generator emitting something other than
    DDL is free to ignore both.
    """

    schema: str = "mart"
    #: Which database the output is for.
    dialect: str = "postgres"
    #: How much of the model the database is asked to police — one of
    #: `enforced`, `asserted`, `none`.
    constraints: str = "enforced"


#: A value a generator's setting may hold: what a TOML scalar reads as.
Setting = str | int | float | bool

#: The types a generator may declare a setting as. Scalars only, because a
#: setting is something a person writes on one line of `varda.toml`, and a
#: generator wanting structure has a model to read it from.
SETTING_TYPES: tuple[type, ...] = (str, int, float, bool)


@dataclass(frozen=True)
class Context:
    """Everything a generator is given, and nothing more.

    Deliberately a closed record rather than the CLI's argument namespace. A
    generator that can reach the CLI's state ends up depending on flags, and
    then its output is a function of how it was invoked rather than of the
    model — which is the property the whole generate-and-compare arrangement
    depends on.

    ``settings`` is this generator's own table from ``varda.toml`` and no
    other generator's, read-only, holding only the keys the generator
    declares. A file is not a flag: it is committed beside the model and
    found from the model's directory, so ``generate`` and ``verify`` read
    the same one and CI reads what a developer does.
    """

    model: Model
    options: Options = field(default_factory=Options)
    settings: Mapping[str, Setting] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True, eq=False)
class Generator:
    """A named producer of files, declaring what it will write.

    ``run`` returns ``{relative path: content}`` and never touches the disk.
    That is what makes generation fail closed: every generator is run and
    every result collected before anything is written, so a failure half way
    through leaves no half-written output tree behind.

    Paths are declared up front, in ``artifacts``, so that two extensions
    claiming the same output file are caught at startup rather than by one
    silently overwriting the other's work.

    A declaration ending in ``/`` claims a directory: everything under it is
    this generator's to write, and nothing under it is anyone else's. It is
    for output whose file names come from the model — one file per table —
    which no declaration written before the model is read could list. The
    claim is the whole directory, and ``varda verify`` reports any file in
    it the run did not produce, so a hand-written file does not belong there.

    ``settings`` names the keys this generator reads from its
    ``[generator.<name>]`` table in ``varda.toml``, and the type of each. A
    key the file gives and this does not declare is refused at load, as is
    a value of the wrong type.
    """

    name: str
    artifacts: tuple[str, ...]
    run: Callable[[Context], dict[str, str]]
    settings: Mapping[str, type] = field(default_factory=dict)

    def declares(self, path: str) -> bool:
        """Flag whether ``path`` is one this generator said it would write."""
        return any(
            path.startswith(claim) if claim.endswith("/") else path == claim
            for claim in self.artifacts
        )

    @property
    def directories(self) -> tuple[str, ...]:
        """The directories this generator claims whole, as declared."""
        return tuple(c for c in self.artifacts if c.endswith("/"))


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Extension:
    """One party's contribution to Varda.

    The only required fields are ``name`` and ``prefix``. An extension that
    declares nothing but a profile is legal and useful: it adds vocabulary
    that ``varda check`` will then accept, which is the smallest thing an
    organization typically wants.

    ``eq=False`` because identity is the right comparison for a registered
    singleton, and ``rules`` holds a mutable :class:`RuleSet`.
    """

    name: str
    prefix: str
    version: str = "0"

    #: Path to a LinkML schema declaring this extension's annotations. Its
    #: `default_prefix` must equal `prefix` — the registry checks, because a
    #: profile that declares annotations under a prefix nobody reads is a
    #: vocabulary that silently never applies.
    profile: pathlib.Path | None = None

    #: Path to a LinkML schema a domain model may import by this
    #: extension's prefix, or ``None`` where there is nothing to import.
    #:
    #: Separate from ``profile`` because the two are read by different
    #: parties. The profile is *read*, by Varda, to learn what annotations
    #: exist. This is *imported*, by a model, so that a range it names
    #: resolves — and a LinkML import is a union, so anything declared here
    #: appears in every generator's output for the importing model. Only an
    #: extension declaring a *type* has something to put here.
    types: pathlib.Path | None = None

    #: Conformance rules. Every code must begin with `rule_tag`.
    rules: RuleSet | None = None

    #: Defaults to `prefix.upper()`. Varda's is `V`.
    rule_tag: str = ""

    #: `{rule code: severity}` — this extension's opinion about the severity
    #: of a rule, including one it does not own. Two extensions disagreeing
    #: about one code is refused rather than resolved; see the registry.
    severity_defaults: dict[str, str] = field(default_factory=dict)

    #: The importable package name.
    package: str = ""

    generators: tuple[Generator, ...] = ()

    #: Where this extension was found — entry point, config file, or
    #: injection. Diagnostic only.
    origin: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        """Derive the rule tag from the prefix when it was not given."""
        if not self.rule_tag:
            object.__setattr__(self, "rule_tag", self.prefix.upper())
