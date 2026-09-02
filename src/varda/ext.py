"""The extension interface — the module a third party imports.

An extension is one party's contribution to Varda: a namespace prefix, a
LinkML profile declaring the annotations that prefix permits, and optionally
a set of conformance rules and generators. Varda itself is an instance of
:class:`Extension` (see :mod:`varda.registry`), which is the test that this
interface is real rather than a second-class bolt-on: if the core can be
expressed through it, a third party is not working around anything.

Everything an extension *runs on* is declared here, and that is a recent
correction rather than a standing property: :class:`Finding` and
:class:`RuleSet` lived in :mod:`varda.rules` while four places in this
package said an extension imports this module and nothing else. What remains
outside is the type of a rule's argument, ``DimensionalModel``, which this
module's own signatures name; whether that read API is public and versioned
is the one boundary question still open.

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
from functools import cached_property
from typing import TYPE_CHECKING, Any

from linkml_runtime.utils.schemaview import SchemaView

from .anns import Reader

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Iterator

    from .model import DimensionalModel

#: A prefix is a namespace, and it becomes a YAML key, a Python identifier and
#: part of a filename. The intersection of what all three accept is narrow.
PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: A rule tag is the letters every code in a set begins with: `V` for Varda,
#: `ACME` for an extension whose prefix is `acme`.
TAG_PATTERN = re.compile(r"^[A-Z]+$")

Severity = str  # "error" | "warning" | "info"

#: Declared here rather than in `rules`, because the registry validates
#: severity overrides too, and two copies of a closed three-value vocabulary
#: is exactly the drift this design exists to prevent.
SEVERITIES = frozenset({"error", "warning", "info"})


class ExtensionError(Exception):
    """An extension is malformed, or collides with another one.

    Always raised, never warned. The cost of raising is a clear error at
    startup; the cost of warning is an estate that half-works for two years
    because nobody reads startup warnings.
    """


@dataclass(frozen=True)
class Finding:
    """One rule violation, against one named subject.

    Here rather than in :mod:`varda.rules` because it is interface rather
    than implementation: every rule an extension writes yields these, so a
    third party cannot avoid the type. The module docstring above claims
    this is the only module an extension imports, and for three releases
    that claim was false in the one place it was load-bearing — the
    documentation page making it, and the fixture written to prove it
    satisfiable, both reached into `varda.rules` for this class and
    :class:`RuleSet`. A boundary stated in four places and contradicted by
    the example beneath it is not a boundary.

    `varda.rules` re-exports both, so a model or an extension written
    against the older import keeps working.
    """

    rule: str
    severity: Severity
    subject: str
    message: str

    #: The file the subject was declared in, when that is not the file the
    #: run was pointed at. Empty otherwise, so the common case — a model in
    #: one file — reads exactly as it always did.
    #:
    #: Defaulted, and filled in by :func:`varda.rules.check` rather than by
    #: the rule. A rule states what is wrong with a subject; where that
    #: subject lives is the same question for all of them, and answering it
    #: sixty times is sixty chances to answer it differently.
    origin: str = ""

    def __str__(self) -> str:
        """Render as a severity line followed by an indented message."""
        mark = {"error": "ERROR", "warning": "WARN ", "info": "INFO "}[
            self.severity
        ]
        where = f"  (imported from {self.origin})" if self.origin else ""
        return (
            f"{mark} {self.rule}  {self.subject}{where}\n        {self.message}"
        )


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


class RuleError(Exception):
    """A rule raised, and the run stopped rather than reporting less.

    The counterpart of the guard around generators in
    :func:`varda.cli.generate`, and it exists for the same reason: the CLI's
    job when somebody else's code fails is to say which code failed, not to
    hand the operator a traceback through a package they did not write.

    Stopping is the deliberate half. A rule that cannot run is a check that
    is not happening, and `varda check` reporting the other forty-eight
    rules and exiting zero would be the tool saying a model conforms when
    part of the question was never asked.
    """


@dataclass(frozen=True)
class Context:
    """Everything a generator is given, and nothing more.

    Deliberately a closed record rather than the CLI's argument namespace. A
    generator that can reach the CLI's state ends up depending on flags, and
    then its output is a function of how it was invoked rather than of the
    model — which is the property the whole generate-and-compare arrangement
    depends on.
    """

    model: DimensionalModel
    source: pathlib.Path
    schema: str = "mart"
    #: Which database the output is for. A name rather than a table, so that
    #: this module stays free of SQL: the SQL generator owns the dialects and
    #: looks this up, and a generator emitting something other than DDL is
    #: free to ignore it.
    dialect: str = "postgres"
    #: How much of the model the database is asked to police — one of
    #: `enforced`, `asserted`, `none`. A name for the same reason `dialect`
    #: is one, and defaulted for a second: a generator written before this
    #: field existed keeps working, and one that emits something other than
    #: DDL has no use for it.
    constraints: str = "enforced"


@dataclass(frozen=True, eq=False)
class Generator:
    """A named producer of files, declaring what it will write.

    ``run`` returns ``{relative path: content}`` and never touches the disk.
    That is what makes the fail-closed guarantee in :func:`varda.cli.generate`
    possible: every generator is run and every result collected before
    anything is written, so a failure half way through leaves no half-written
    output tree behind.

    Paths are declared up front, in ``artifacts``, so that two extensions
    claiming the same output file are caught at startup rather than by one
    silently overwriting the other's work.

    ``eq=False`` gives identity comparison, because ``run`` is a function and
    two distinct generators wrapping the same function are still two
    generators.
    """

    name: str
    artifacts: tuple[str, ...]
    run: Callable[[Context], dict[str, str]]


@dataclass(frozen=True, eq=False)
class Extension:
    """One party's contribution to Varda.

    The only required fields are ``name`` and ``prefix``. An extension that
    declares nothing but a profile is legal and useful: it adds vocabulary
    that ``varda check`` will then accept, which is the smallest thing an
    organization typically wants.

    ``eq=False`` because ``rules`` holds a mutable :class:`RuleSet`, which
    makes the dataclass unhashable under the default ``eq``; identity is the
    right comparison for a registered singleton anyway.
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
    #: parties for different reasons. The profile is *read* — by Varda,
    #: off disk, to learn what annotations exist. This is *imported* — by
    #: a model, so that a range it names resolves.
    #:
    #: A LinkML import is a union: everything the imported schema declares
    #: becomes part of the importing schema and appears in the output of
    #: every generator that walks its classes. So an extension that only
    #: declares annotations sets nothing here, and a model cannot import
    #: it even by mistake. Only an extension declaring a *type* has
    #: something a model needs.
    types: pathlib.Path | None = None

    #: Conformance rules. Every code must begin with `rule_tag`.
    rules: RuleSet | None = None

    #: Defaults to `prefix.upper()`. Varda's is `V`, not by special-casing
    #: but because tag uniqueness is checked across all extensions and Varda
    #: registers first.
    rule_tag: str = ""

    #: `{rule code: severity}` — this extension's opinion about the severity
    #: of a rule, including one it does not own. Two extensions disagreeing
    #: about one code is refused rather than resolved; see the registry.
    severity_defaults: dict[str, Severity] = field(default_factory=dict)

    #: The importable package name, used to resolve the profile for the
    #: LinkML import map so a domain model can write `imports: - acme`.
    package: str = ""

    generators: tuple[Generator, ...] = ()

    #: Where this extension was found — entry point, config file, or
    #: injection. Excluded from comparison because it is diagnostic only.
    origin: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        """Derive the rule tag from the prefix when it was not given."""
        if not self.rule_tag:
            object.__setattr__(self, "rule_tag", self.prefix.upper())

    @staticmethod
    def reader(prefix: str) -> Reader:
        """Build a reader for a namespace.

        Exposed here so an extension never imports :mod:`varda.anns`: the
        whole public surface is this module, and a third party reading its
        own annotations is the most common thing it will do.
        """
        return Reader(prefix)

    @property
    def anns(self) -> Reader:
        """A reader bound to this extension's own prefix."""
        return Reader(self.prefix)

    @cached_property
    def profile_view(self) -> SchemaView | None:
        """The parsed profile, or ``None`` if this extension declares none."""
        return None if self.profile is None else SchemaView(str(self.profile))

    @cached_property
    def types_view(self) -> SchemaView | None:
        """The parsed types schema, or ``None`` where there is none."""
        return None if self.types is None else SchemaView(str(self.types))

    @cached_property
    def profile_version(self) -> str | None:
        """The version string the profile declares, if it declares one."""
        view = self.profile_view
        if view is None:
            return None
        version: Any = view.schema.version
        return None if version is None else str(version)
