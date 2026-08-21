"""The extension interface — the only module a third party imports.

An extension is one party's contribution to Varda: a namespace prefix, a
LinkML profile declaring the annotations that prefix permits, and optionally
a set of conformance rules and generators. Varda itself is an instance of
:class:`Extension` (see :mod:`varda.registry`), which is the test that this
interface is real rather than a second-class bolt-on: if the core can be
expressed through it, a third party is not working around anything.

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
    from collections.abc import Callable

    from .model import DimensionalModel
    from .rules import RuleSet

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


@dataclass(frozen=True, eq=False)
class Generator:
    """A named producer of files, declaring what it will write.

    ``run`` returns ``{relative path: content}`` and never touches the disk.
    That is what makes the fail-closed guarantee in :func:`varda.cli.generate`
    possible: every generator is run and every result collected before
    anything is written, so a failure half way through leaves no half-written
    output tree behind.

    Paths are declared up front, in ``artefacts``, so that two extensions
    claiming the same output file are caught at startup rather than by one
    silently overwriting the other's work.

    ``eq=False`` gives identity comparison, because ``run`` is a function and
    two distinct generators wrapping the same function are still two
    generators.
    """

    name: str
    artefacts: tuple[str, ...]
    run: Callable[[Context], dict[str, str]]


@dataclass(frozen=True, eq=False)
class Extension:
    """One party's contribution to Varda.

    The only required fields are ``name`` and ``prefix``. An extension that
    declares nothing but a profile is legal and useful: it adds vocabulary
    that ``varda check`` will then accept, which is the smallest thing an
    organisation typically wants.

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
    def profile_version(self) -> str | None:
        """The version string the profile declares, if it declares one."""
        view = self.profile_view
        if view is None:
            return None
        version: Any = view.schema.version
        return None if version is None else str(version)
