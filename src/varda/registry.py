"""Discovery, validation and lookup of the active extension set.

Three ways an extension becomes active, in increasing order of locality:

1. Varda itself, always, as an ordinary :class:`Extension`.
2. An installed package advertising the ``varda.extensions`` entry point.
3. A ``varda.toml`` found by searching upward from the working directory.

The third exists because the second requires publishing a package, and the
smallest useful extension — a handful of extra annotations with no code
behind them — should not require that. An organization can add vocabulary
with a YAML file and three lines of TOML.

Everything here is validated once, at first use, and then cached. The
validation is deliberately fail-closed: a malformed or colliding extension
raises rather than being skipped, because an extension silently not loading
looks exactly like an extension whose rules all pass.
"""

from __future__ import annotations

import contextlib
import pathlib
import tomllib
from functools import cache
from importlib import import_module
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Literal, get_args

from .anns import Reader
from .ext import (
    PREFIX_PATTERN,
    SEVERITIES,
    TAG_PATTERN,
    Extension,
    ExtensionError,
    Generator,
    Severity,
)
from .model import PROFILE

if TYPE_CHECKING:
    from collections.abc import Iterator

Target = Literal["table", "column"]
TARGETS: tuple[Target, ...] = get_args(Target)

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

#: Extensions injected by :func:`using`, for tests and for programmatic use.
_injected: tuple[Extension, ...] = ()


@cache
def varda_extension() -> Extension:
    """Varda itself, as an extension.

    The core going through the same interface is the test that the interface
    is real. If Varda needed a privilege the mechanism does not offer, a third
    party would find that out the hard way; this way it is a load error here.
    """
    from . import (  # noqa: PLC0415
        __version__,
        generators,
        rules,
    )

    return Extension(
        name="varda",
        prefix="varda",
        version=__version__,
        profile=PROFILE,
        rules=rules.RULES,
        rule_tag="V",
        package="varda",
        generators=generators.BUILTIN,
        origin="built in",
    )


@cache
def extensions() -> tuple[Extension, ...]:
    """Every active extension, validated, with Varda first."""
    found = [varda_extension(), *_from_entry_points(), *_from_config()]
    found.extend(_injected)
    _validate(found)
    return tuple(found)


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
        out.append(
            Extension(**{**obj.__dict__, "origin": f"entry point {ep.name}"})
            if not obj.origin
            else obj
        )
    return out


def find_config(start: pathlib.Path | None = None) -> pathlib.Path | None:
    """Locate ``varda.toml`` by searching upward from ``start``.

    Upward rather than at a fixed location, so the answer does not depend on
    which subdirectory a command was run from — the single most common way a
    tool behaves differently for two people on the same repository.
    """
    import os  # noqa: PLC0415

    override = os.environ.get("VARDA_CONFIG")
    if override:
        return pathlib.Path(override)
    here = (start or pathlib.Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def config() -> dict[str, Any]:
    """Read ``varda.toml``, or an empty mapping when there is none."""
    path = find_config()
    if path is None or not path.is_file():
        return {}
    with path.open("rb") as handle:
        loaded: dict[str, Any] = tomllib.load(handle)
    return loaded


def _from_config() -> list[Extension]:
    """Build extensions declared in ``varda.toml``.

    Two forms. ``extensions = ["acme_ext"]`` imports a module and takes its
    ``EXTENSION`` attribute — the form for an extension with code behind it.
    ``[[extension]]`` tables declare one inline from a name, a prefix and a
    profile path — the form for an organization that only wants vocabulary.
    """
    path = find_config()
    if path is None:
        return []
    data = config()
    out: list[Extension] = []
    where = f"{CONFIG_NAME} ({path})"

    for spec in data.get("extensions", []):
        module, _, attr = str(spec).partition(":")
        try:
            loaded = import_module(module)
        except ImportError as exc:
            msg = f"{where}: cannot import {module!r}: {exc}"
            raise ExtensionError(msg) from exc
        obj = getattr(loaded, attr or "EXTENSION", None)
        if not isinstance(obj, Extension):
            msg = (
                f"{where}: {spec!r} does not name an Extension "
                f"(got {type(obj).__name__})"
            )
            raise ExtensionError(msg)
        out.append(
            obj
            if obj.origin
            else Extension(**{**obj.__dict__, "origin": where})
        )

    out.extend(_inline(e, path, where) for e in data.get("extension", []))
    return out


def _inline(
    entry: dict[str, Any], config_path: pathlib.Path, where: str
) -> Extension:
    """Build an extension from a ``[[extension]]`` table."""
    missing = [k for k in ("name", "prefix") if k not in entry]
    if missing:
        msg = f"{where}: [[extension]] is missing {', '.join(missing)}"
        raise ExtensionError(msg)
    profile = entry.get("profile")
    resolved = None
    if profile is not None:
        resolved = (config_path.parent / str(profile)).resolve()
        if not resolved.is_file():
            msg = f"{where}: profile {profile!r} does not exist"
            raise ExtensionError(msg)
    return Extension(
        name=str(entry["name"]),
        prefix=str(entry["prefix"]),
        version=str(entry.get("version", "0")),
        profile=resolved,
        severity_defaults=_str_map(entry.get("severity", {})),
        origin=where,
    )


def _str_map(value: Any) -> dict[str, str]:
    """Coerce a TOML table to ``dict[str, str]``.

    TOML gives back ``dict[str, Any]``, and the alternative to this three-line
    function is a ``type: ignore`` on the call site — which suppresses the one
    check that would notice a table of the wrong shape.
    """
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


@contextlib.contextmanager
def using(*extra: Extension) -> Iterator[tuple[Extension, ...]]:
    """Activate extra extensions for the duration of a block.

    For tests and for programmatic use. Resets the caches on the way in and
    on the way out, so a block never sees a lookup computed under a different
    extension set.
    """
    global _injected  # noqa: PLW0603
    previous = _injected
    _injected = (*previous, *extra)
    reset_caches()
    try:
        yield extensions()
    finally:
        _injected = previous
        reset_caches()


def reset_caches() -> None:
    """Clear every cached lookup in this module."""
    for fn in (
        extensions,
        declared_annotations,
        annotation_enum,
        prefixes,
        permitted,
        profile_filename,
        severities,
        generators,
        _enums_of,
    ):
        fn.cache_clear()


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
        for code, *_ in ext.rules.rules:
            if code in seen:
                msg = (
                    f"rule {code} is registered by both {seen[code]} and "
                    f"{ext.name}"
                )
                raise ExtensionError(msg)
            seen[code] = ext.name


def _check_artifacts(found: list[Extension]) -> None:
    """Refuse two generators claiming one name or one output path."""
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
                if path in paths:
                    msg = (
                        f"{path!r} is written by both {paths[path]} and "
                        f"{gen.name}; one would silently overwrite the other"
                    )
                    raise ExtensionError(msg)
                paths[path] = gen.name


def _check_profiles(found: list[Extension]) -> None:
    """Refuse a profile whose namespace does not match its extension.

    A profile declaring annotations under a prefix nobody reads is a
    vocabulary that silently never applies — every annotation it permits
    still fails V001.
    """
    for ext in found:
        view = ext.profile_view
        if view is None:
            continue
        declared = str(view.schema.default_prefix or "")
        if declared != ext.prefix:
            msg = (
                f"{ext.name}: profile declares default_prefix {declared!r} "
                f"but the extension's prefix is {ext.prefix!r}"
            )
            raise ExtensionError(msg)
        reader = Reader(ext.prefix)
        for name, cls in view.schema.classes.items():
            target = reader.get(cls, "applies_to")
            if target is None:
                continue
            if target not in TARGETS:
                msg = (
                    f"{ext.name}: class {name} declares applies_to "
                    f"{target!r}; expected one of {', '.join(TARGETS)}"
                )
                raise ExtensionError(msg)


def _check_vocabulary_collisions(found: list[Extension]) -> None:
    """Refuse an extension that redeclares something Varda already owns.

    This is the mechanical form of the design's central prohibition:
    extensions add, they never redefine. An extension that redeclares
    ``Additivity`` is not extending the vocabulary, it is forking it — and
    the fork would be invisible, because both schemas parse.
    """
    owners: dict[str, str] = {}
    for ext in found:
        view = ext.profile_view
        if view is None:
            continue
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
    machines that nothing in either repository explains. There is no
    principled winner here, so the tool declines to invent one and names
    both parties instead.
    """
    claims: dict[str, tuple[str, Severity]] = {}
    for ext in found:
        for code, severity in ext.severity_defaults.items():
            if severity not in SEVERITIES:
                msg = (
                    f"{ext.name}: severity {severity!r} for {code} is not "
                    f"one of {', '.join(sorted(SEVERITIES))}"
                )
                raise ExtensionError(msg)
            prior = claims.get(code)
            if prior is not None and prior[1] != severity:
                msg = (
                    f"{prior[0]} wants {code} at {prior[1]!r} and "
                    f"{ext.name} wants {severity!r}. Nothing here can "
                    f"adjudicate that — set it in {CONFIG_NAME}, which "
                    f"overrides both"
                )
                raise ExtensionError(msg)
            claims[code] = (ext.name, severity)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


@cache
def declared_annotations(target: Target) -> frozenset[str]:
    """Every annotation tag legal on ``target``, across all extensions.

    Full tags — ``varda:role``, ``acme:cost_center`` — rather than bare names,
    so a tag is checked against the extension that owns its prefix rather than
    against the union of everybody's vocabulary.
    """
    out: set[str] = set()
    for ext, cls in _annotation_classes(target):
        for attr in cls.attributes or {}:
            out.add(f"{ext.prefix}:{attr}")
    return frozenset(out)


@cache
def annotation_enum(target: Target, tag: str) -> str | None:
    """Name the enum an annotation's values must come from, if any."""
    prefix, _, name = tag.partition(":")
    for ext, cls in _annotation_classes(target):
        if ext.prefix != prefix:
            continue
        attr = (cls.attributes or {}).get(name)
        if attr is None:
            continue
        rng = str(attr.range or "")
        if rng in _enums_of(ext.prefix):
            return rng
    return None


@cache
def annotation_shape(target: Target, tag: str) -> dict[str, str] | None:
    """Name the fields a structured annotation may carry, and their ranges.

    ``None`` when the annotation's range is a scalar or an enum, which is the
    common case — only an annotation whose range is a class declared in the
    same profile has a shape to check against.
    """
    prefix, _, name = tag.partition(":")
    for ext, cls in _annotation_classes(target):
        if ext.prefix != prefix:
            continue
        attr = (cls.attributes or {}).get(name)
        if attr is not None:
            return structured_shape(prefix, str(attr.range or ""))
    return None


@cache
def structured_shape(prefix: str, name: str) -> dict[str, str] | None:
    """Name the fields of one structured range, and the range of each.

    ``None`` when the extension owning ``prefix`` declares no class by that
    name, which is how a scalar range and a typo are told apart.
    """
    for ext in extensions():
        if ext.prefix != prefix or ext.profile_view is None:
            continue
        cls = ext.profile_view.schema.classes.get(name)
        if cls is None:
            return None
        return {
            str(field): str(attr.range or "")
            for field, attr in (cls.attributes or {}).items()
        }
    return None


def _annotation_classes(target: Target) -> Iterator[tuple[Extension, Any]]:
    """Walk the annotation classes that apply to one kind of object."""
    for ext in extensions():
        view = ext.profile_view
        if view is None:
            continue
        reader = Reader(ext.prefix)
        for cls in view.schema.classes.values():
            if reader.get(cls, "applies_to") == target:
                yield ext, cls


@cache
def _enums_of(prefix: str) -> frozenset[str]:
    """Every enum name declared by the extension owning ``prefix``."""
    for ext in extensions():
        if ext.prefix == prefix and ext.profile_view is not None:
            return frozenset(ext.profile_view.schema.enums)
    return frozenset()


@cache
def prefixes() -> frozenset[str]:
    """Every active extension's prefix."""
    return frozenset(e.prefix for e in extensions())


@cache
def permitted(enum_name: str) -> tuple[str, ...]:
    """Return an enum's permissible values.

    Resolved from whichever profile declares the enum, so an extension's own
    enums are enforced by V002 exactly as Varda's are.
    """
    for ext in extensions():
        view = ext.profile_view
        if view is None or enum_name not in view.schema.enums:
            continue
        enum = view.schema.enums[enum_name]
        return tuple(str(v) for v in (enum.permissible_values or {}))
    return ()


@cache
def profile_filename(prefix: str) -> str:
    """Name the profile file an annotation of this prefix belongs in.

    Used in V001's message, so the error says where to go rather than only
    what is wrong.
    """
    for ext in extensions():
        if ext.prefix == prefix and ext.profile is not None:
            return ext.profile.name
    return f"the {prefix} profile"


@cache
def severities() -> dict[str, Severity]:
    """Resolve severity overrides: extension defaults, then ``varda.toml``.

    The repository's word is final, and deliberately so. An extension's
    ``severity_defaults`` is an opinion shipped by a party who cannot see this
    codebase; the config file is written by people who can.
    """
    out: dict[str, Severity] = {}
    for ext in extensions():
        out.update(ext.severity_defaults)
    out.update(_str_map(config().get("severity", {})))
    return out


@cache
def generators() -> tuple[Generator, ...]:
    """Every registered generator, sorted by name."""
    out: list[Generator] = []
    for ext in extensions():
        out.extend(ext.generators)
    return tuple(sorted(out, key=lambda g: g.name))


def exemptions() -> list[str]:
    """Rules ``varda.toml`` asks to skip."""
    return [str(c) for c in config().get("exempt", [])]


def importmap() -> dict[str, str]:
    """Map symbolic profile imports to paths on this machine.

    Lets a domain model write ``imports: - varda`` instead of a relative path
    into site-packages. Not cached and never written to disk: the values are
    absolute paths, so a committed map is wrong on every other machine.
    """
    out: dict[str, str] = {}
    for ext in extensions():
        if ext.profile is not None:
            out[ext.prefix] = str(ext.profile.with_suffix(""))
    return out
