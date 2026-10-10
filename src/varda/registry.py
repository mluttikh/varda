"""Discovery and validation of the active extension set.

Three ways an extension becomes active, in increasing order of locality:

1. Varda itself, always, as an ordinary :class:`~varda.ext.Extension`.
2. An installed package advertising the ``varda.extensions`` entry point.
3. A ``varda.toml`` found by searching upward from the model's directory.

The third exists because the second requires publishing a package, and the
smallest useful extension — a handful of extra annotations with no code
behind them — should not require that. An organization can add vocabulary
with a YAML file and three lines of TOML.

A :class:`Registry` is one validated extension set, built once and never
changed. Validation is fail-closed: a malformed or colliding extension
raises rather than being skipped, because an extension silently not loading
looks exactly like an extension whose rules all pass.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import tomllib
from dataclasses import dataclass, field
from functools import cache, cached_property
from importlib import import_module
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

from . import _linkml
from .ext import (
    PREFIX_PATTERN,
    TAG_PATTERN,
    Extension,
    ExtensionError,
    Generator,
    Rule,
    Severity,
)
from .vocabulary import TARGETS, Vocabulary

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

#: The vocabulary — annotations, enums, and the shapes of the structured
#: ones. Read straight off disk. A domain model never imports it; see TYPES.
PROFILE = pathlib.Path(__file__).parent / "profile" / "varda.yaml"

#: The schema a domain model imports, and all that `imports: - varda`
#: resolves to. One type, because a range must resolve for a file to be a
#: legal LinkML schema and LinkML has no `uuid`.
TYPES = pathlib.Path(__file__).parent / "profile" / "types.yaml"

#: Prefixes an extension may not claim. Varda's own, plus the ones LinkML and
#: the surrounding standards already use — an extension that claimed `skos`
#: would produce annotations that look like standard vocabulary and are not.
RESERVED_PREFIXES = frozenset(
    {
        "varda",
        "linkml",
        "skos",
        "dcterms",
        "dc",
        "owl",
        "rdf",
        "rdfs",
        "sh",
        "schema",
        "xsd",
    }
)

CONFIG_NAME = "varda.toml"
ENTRY_POINT_GROUP = "varda.extensions"

#: Every key ``varda.toml`` may carry. A key outside it is refused rather
#: than ignored: `exempts = [...]` written for `exempt` is a rule suppressed
#: nowhere while reading exactly like a rule suppressed here.
CONFIG_KEYS = frozenset({"extensions", "extension", "severity", "exempt"})


@cache
def varda_extension() -> Extension:
    """Varda itself, as an extension.

    The core going through the same interface is the test that the interface
    is real. If Varda needed a privilege the mechanism does not offer, a third
    party would find that out the hard way; this way it is a load error here.

    Cached because it is a constant: the same object under every extension
    set, with nothing in it that a registry could change.
    """
    from . import __version__, generators, rules  # noqa: PLC0415 — cycle

    return Extension(
        name="varda",
        prefix="varda",
        version=__version__,
        profile=PROFILE,
        types=TYPES,
        rules=rules.RULES,
        rule_tag="V",
        package="varda",
        generators=generators.BUILTIN,
        origin="built in",
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """A ``varda.toml``, read and checked, or the absence of one."""

    path: pathlib.Path | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: pathlib.Path | None) -> Config:
        """Read a config file, refusing a key nothing reads."""
        if path is None or not path.is_file():
            return cls()
        with path.open("rb") as handle:
            loaded: dict[str, Any] = tomllib.load(handle)
        unknown = sorted(set(loaded) - CONFIG_KEYS)
        if unknown:
            msg = (
                f"{CONFIG_NAME} ({path}): unknown key(s) "
                f"{', '.join(repr(k) for k in unknown)}; "
                f"expected one of {', '.join(sorted(CONFIG_KEYS))}"
            )
            raise ExtensionError(msg)
        return cls(path=path, data=loaded)

    @property
    def where(self) -> str:
        """Name the file, for an error message."""
        return f"{CONFIG_NAME} ({self.path})"


def find_config(start: pathlib.Path | None = None) -> pathlib.Path | None:
    """Locate ``varda.toml`` by searching upward from ``start``.

    ``start`` is the model's directory when there is a model, so the answer
    depends on where the model is and not on where the command was run —
    the single most common way a tool behaves differently for two people on
    the same repository. ``VARDA_CONFIG`` overrides the search.
    """
    override = os.environ.get("VARDA_CONFIG")
    if override:
        return pathlib.Path(override)
    here = (start or pathlib.Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class Registry:
    """One validated extension set, and everything derived from it.

    Immutable once built. Two registries never share state, so a test, a
    notebook and a long-running service can each hold their own without
    anything to reset between them.
    """

    def __init__(
        self,
        extensions: Sequence[Extension],
        config: Config | None = None,
    ) -> None:
        """Validate an extension set, Varda first, and hold it."""
        found = list(extensions)
        _validate(found)
        self.extensions: tuple[Extension, ...] = tuple(found)
        self.config = config or Config()
        self.vocabulary = Vocabulary(self.extensions)

    @classmethod
    def discover(
        cls,
        start: pathlib.Path | None = None,
        *,
        config: pathlib.Path | None = None,
    ) -> Registry:
        """Varda, every installed extension, and the nearest ``varda.toml``.

        ``config`` names the file outright; otherwise it is searched for
        upward from ``start``.
        """
        loaded = Config.load(config or find_config(start))
        return cls(
            [varda_extension(), *_from_entry_points(), *_from_config(loaded)],
            loaded,
        )

    @classmethod
    def of(
        cls,
        *extensions: Extension,
        config: Config | None = None,
    ) -> Registry:
        """Varda plus exactly these, and nothing installed or configured.

        For tests and for programmatic use, where what is active should be
        what the caller said and not what happens to be on the machine.
        """
        loaded = config or Config()
        return cls(
            [varda_extension(), *extensions, *_from_config(loaded)], loaded
        )

    @cached_property
    def rules(self) -> tuple[Rule, ...]:
        """Every registered rule, from every extension, sorted by code.

        Sorted by code alone rather than by extension, because a reader
        looking for V703 wants it where V702 was, and an operator reading a
        run wants the same ordering every time regardless of what is
        installed.
        """
        out = [
            rule
            for ext in self.extensions
            if ext.rules is not None
            for rule in ext.rules.rules
        ]
        return tuple(sorted(out, key=lambda r: r.code))

    @cached_property
    def generators(self) -> tuple[Generator, ...]:
        """Every registered generator, sorted by name."""
        out = [g for ext in self.extensions for g in ext.generators]
        return tuple(sorted(out, key=lambda g: g.name))

    @cached_property
    def severities(self) -> dict[str, Severity]:
        """Resolve severity overrides: extension defaults, then the config.

        The repository's word is final, and deliberately so. An extension's
        ``severity_defaults`` is an opinion shipped by a party who cannot see
        this codebase; the config file is written by people who can.
        """
        out: dict[str, Severity] = {}
        for ext in self.extensions:
            for code, severity in ext.severity_defaults.items():
                out[code] = Severity.parse(severity, ext.name, code)
        wanted = _str_map(self.config.data.get("severity", {}))
        for code, severity in sorted(wanted.items()):
            out[code] = Severity.parse(severity, CONFIG_NAME, code)
        return out

    @property
    def exemptions(self) -> tuple[str, ...]:
        """Rules ``varda.toml`` asks to skip."""
        return tuple(str(c) for c in self.config.data.get("exempt", []))

    @cached_property
    def importmap(self) -> dict[str, str]:
        """Map symbolic imports to paths on this machine.

        Lets a domain model write ``imports: - varda`` instead of a relative
        path into site-packages. Never written to disk: the values are
        absolute paths, so a committed map is wrong on every other machine.

        Built from ``Extension.types``, never from ``Extension.profile``. A
        LinkML import is a union, so exposing a profile here would put its
        annotation classes into the importing schema, where every stock
        generator that walks the class list emits them.
        """
        return {
            ext.prefix: str(ext.types.with_suffix(""))
            for ext in self.extensions
            if ext.types is not None
        }

    def rule(self, code: str) -> Rule | None:
        """Look one rule up by code."""
        return next((r for r in self.rules if r.code == code), None)

    def owner(self, code: str) -> str:
        """Name the extension that registered a rule code."""
        for ext in self.extensions:
            if ext.rules is not None and any(
                r.code == code for r in ext.rules.rules
            ):
                return ext.name
        return "no active extension"

    def unknown_codes(self, names: Iterable[str]) -> tuple[str, ...]:
        """Name the given codes that no active extension registers.

        Used to catch an exemption that has outlived the rule it suppressed
        — a suppression nobody notices has stopped applying.
        """
        known = {r.code for r in self.rules}
        return tuple(sorted(set(names) - known))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _from_entry_points() -> list[Extension]:
    """Load extensions advertised by installed packages."""
    out: list[Extension] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
        except Exception as exc:
            msg = f"entry point {ep.name!r} could not be loaded: {exc}"
            raise ExtensionError(msg) from exc
        if not isinstance(obj, Extension):
            msg = (
                f"entry point {ep.name!r} resolved to {type(obj).__name__}, "
                f"not an Extension"
            )
            raise ExtensionError(msg)
        where = f"entry point {ep.name}"
        out.append(obj if obj.origin else _stamped(obj, where))
    return out


def _stamped(ext: Extension, origin: str) -> Extension:
    """Copy an extension, recording where it was found."""
    return dataclasses.replace(ext, origin=origin)


def _from_config(config: Config) -> list[Extension]:
    """Build the extensions a ``varda.toml`` declares.

    Two forms. ``extensions = ["acme_ext"]`` imports a module and takes its
    ``EXTENSION`` attribute — the form for an extension with code behind it.
    ``[[extension]]`` tables declare one inline from a name, a prefix and a
    profile path — the form for an organization that only wants vocabulary.
    """
    if config.path is None:
        return []
    out: list[Extension] = []
    for spec in config.data.get("extensions", []):
        module, _, attr = str(spec).partition(":")
        try:
            loaded = import_module(module)
        except ImportError as exc:
            msg = f"{config.where}: cannot import {module!r}: {exc}"
            raise ExtensionError(msg) from exc
        obj = getattr(loaded, attr or "EXTENSION", None)
        if not isinstance(obj, Extension):
            msg = (
                f"{config.where}: {spec!r} does not name an Extension "
                f"(got {type(obj).__name__})"
            )
            raise ExtensionError(msg)
        out.append(obj if obj.origin else _stamped(obj, config.where))
    out.extend(_inline(e, config) for e in config.data.get("extension", []))
    return out


def _inline(entry: dict[str, Any], config: Config) -> Extension:
    """Build an extension from a ``[[extension]]`` table."""
    missing = [k for k in ("name", "prefix") if k not in entry]
    if missing:
        msg = f"{config.where}: [[extension]] is missing {', '.join(missing)}"
        raise ExtensionError(msg)
    profile = entry.get("profile")
    resolved = None
    if profile is not None and config.path is not None:
        resolved = (config.path.parent / str(profile)).resolve()
        if not resolved.is_file():
            msg = f"{config.where}: profile {profile!r} does not exist"
            raise ExtensionError(msg)
    return Extension(
        name=str(entry["name"]),
        prefix=str(entry["prefix"]),
        version=str(entry.get("version", "0")),
        profile=resolved,
        severity_defaults=_str_map(entry.get("severity", {})),
        origin=config.where,
    )


def _str_map(value: Any) -> dict[str, str]:
    """Coerce a TOML table to ``dict[str, str]``."""
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(found: list[Extension]) -> None:
    """Refuse a set of extensions that cannot coexist."""
    _check_prefixes(found)
    _check_rule_tags(found)
    _check_rule_codes(found)
    _check_artifacts(found)
    _check_profiles(found)
    _check_types(found)
    _check_vocabulary_collisions(found)
    _check_severity_defaults(found)


def _check_prefixes(found: list[Extension]) -> None:
    seen: dict[str, Extension] = {}
    for ext in found:
        if not PREFIX_PATTERN.fullmatch(ext.prefix):
            msg = (
                f"{ext.name}: prefix {ext.prefix!r} must be lowercase, start "
                f"with a letter, and contain only letters, digits and "
                f"underscores"
            )
            raise ExtensionError(msg)
        if ext.prefix in RESERVED_PREFIXES and ext.name != "varda":
            msg = f"{ext.name}: prefix {ext.prefix!r} is reserved"
            raise ExtensionError(msg)
        if ext.prefix in seen:
            msg = (
                f"prefix {ext.prefix!r} is claimed by both "
                f"{seen[ext.prefix].name} ({seen[ext.prefix].origin}) and "
                f"{ext.name} ({ext.origin})"
            )
            raise ExtensionError(msg)
        seen[ext.prefix] = ext


def _check_rule_tags(found: list[Extension]) -> None:
    seen: dict[str, Extension] = {}
    for ext in found:
        tag = ext.rule_tag
        if not TAG_PATTERN.fullmatch(tag):
            msg = f"{ext.name}: rule tag {tag!r} must be uppercase letters"
            raise ExtensionError(msg)
        if ext.rules is not None and ext.rules.tag != tag:
            msg = (
                f"{ext.name}: declares rule tag {tag!r} but its RuleSet says "
                f"{ext.rules.tag!r}"
            )
            raise ExtensionError(msg)
        if tag in seen:
            msg = (
                f"rule tag {tag!r} is claimed by both {seen[tag].name} and "
                f"{ext.name}; codes would be ambiguous"
            )
            raise ExtensionError(msg)
        seen[tag] = ext


def _check_rule_codes(found: list[Extension]) -> None:
    seen: dict[str, str] = {}
    for ext in found:
        if ext.rules is None:
            continue
        for rule in ext.rules.rules:
            if rule.code in seen:
                msg = (
                    f"rule {rule.code} is registered by both "
                    f"{seen[rule.code]} and {ext.name}"
                )
                raise ExtensionError(msg)
            seen[rule.code] = ext.name


def _escapes(path: str) -> bool:
    r"""Flag an artifact path that would not stay inside ``--out``.

    Checked under both path flavors rather than this machine's. A generator
    declaring ``C:\out\x`` is absolute on Windows and an ordinary relative
    name on POSIX, and the same package is installed on both — so a path is
    refused if either flavor would let it out.

    ``..`` is refused rather than resolved. `sql/../mart.sql` stays inside
    and is still a declaration nobody meant to write, and normalizing it
    would accept `sql/../../mart.sql` for the length of one more segment.
    """
    flavors = (pathlib.PurePosixPath(path), pathlib.PureWindowsPath(path))
    return not path or any(
        p.is_absolute() or p.anchor or ".." in p.parts for p in flavors
    )


def _check_artifacts(found: list[Extension]) -> None:
    """Refuse two generators claiming one name or one output path.

    Also refuses a path that would be written outside ``--out``, at load,
    before anything runs.
    """
    names: dict[str, str] = {}
    paths: dict[str, str] = {}
    for ext in found:
        for gen in ext.generators:
            if gen.name in names:
                msg = (
                    f"generator {gen.name!r} is registered by both "
                    f"{names[gen.name]} and {ext.name}"
                )
                raise ExtensionError(msg)
            names[gen.name] = ext.name
            for path in gen.artifacts:
                if _escapes(path):
                    msg = (
                        f"{gen.name} declares artifact {path!r}, which does "
                        f"not stay inside the output directory; an artifact "
                        f"path is relative and contains no '..'"
                    )
                    raise ExtensionError(msg)
                if path in paths:
                    msg = (
                        f"{path!r} is written by both {paths[path]} and "
                        f"{gen.name}; one would silently overwrite the other"
                    )
                    raise ExtensionError(msg)
                paths[path] = gen.name


def _check_types(found: list[Extension]) -> None:
    """Refuse a types schema that declares anything a model must not import.

    A LinkML import is a union. Whatever this schema declares becomes part of
    every model importing it and is emitted by every stock generator that
    walks the class list — so a class or an enum here is not vocabulary a
    model gains, it is output a model did not ask for.
    """
    for ext in found:
        if ext.types is None:
            continue
        view = _linkml.schema(ext.types)
        declared = sorted(view.schema.classes) + sorted(view.schema.enums)
        if declared:
            msg = (
                f"{ext.name}: {ext.types.name} is imported by domain models "
                f"and may declare types only, but declares "
                f"{', '.join(repr(n) for n in declared)}. Move them to the "
                f"profile, which Varda reads without a model importing it."
            )
            raise ExtensionError(msg)


def _check_profiles(found: list[Extension]) -> None:
    """Refuse a profile whose namespace does not match its extension.

    A profile declaring annotations under a prefix nobody reads is a
    vocabulary that silently never applies — every annotation it permits
    still fails V001.
    """
    for ext in found:
        if ext.profile is None:
            continue
        view = _linkml.schema(ext.profile)
        declared = str(view.schema.default_prefix or "")
        if declared != ext.prefix:
            msg = (
                f"{ext.name}: profile declares default_prefix {declared!r} "
                f"but the extension's prefix is {ext.prefix!r}"
            )
            raise ExtensionError(msg)
        for name, cls in view.schema.classes.items():
            target = _linkml.text(cls, f"{ext.prefix}:applies_to")
            if target is None:
                continue
            if target not in TARGETS:
                msg = (
                    f"{ext.name}: class {name} declares applies_to "
                    f"{target!r}; expected one of {', '.join(TARGETS)}"
                )
                raise ExtensionError(msg)


def _check_vocabulary_collisions(found: list[Extension]) -> None:
    """Refuse an extension that redeclares something another one owns.

    The mechanical form of the design's central prohibition: extensions
    add, they never redefine. An extension redeclaring ``Additivity`` is not
    extending the vocabulary, it is forking it — and the fork would be
    invisible, because both schemas parse.
    """
    owners: dict[str, str] = {}
    for ext in found:
        if ext.profile is None:
            continue
        view = _linkml.schema(ext.profile)
        for kind, names in (
            ("enum", view.schema.enums),
            ("class", view.schema.classes),
        ):
            for name in names:
                key = f"{kind} {name}"
                if key in owners and owners[key] != ext.name:
                    msg = (
                        f"{ext.name} redeclares {key}, which belongs to "
                        f"{owners[key]}; extensions add, they never redefine"
                    )
                    raise ExtensionError(msg)
                owners[key] = ext.name


def _check_severity_defaults(found: list[Extension]) -> None:
    """Refuse two extensions stating different severities for one rule.

    Resolving this by load order would make the answer depend on which
    package happened to be discovered first — a difference between two
    machines that nothing in either repository explains.
    """
    claims: dict[str, tuple[str, Severity]] = {}
    for ext in found:
        for code, written in ext.severity_defaults.items():
            severity = Severity.parse(written, ext.name, code)
            prior = claims.get(code)
            if prior is not None and prior[1] != severity:
                msg = (
                    f"{prior[0]} wants {code} at {prior[1].value!r} and "
                    f"{ext.name} wants {severity.value!r}. Nothing here can "
                    f"adjudicate that — set it in {CONFIG_NAME}, which "
                    f"overrides both"
                )
                raise ExtensionError(msg)
            claims[code] = (ext.name, severity)
