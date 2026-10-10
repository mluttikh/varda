"""What the active profiles declare.

The read-only half of the registry, and the half rules need: which
annotations are legal where, which enum a value must come from, what fields
a structured annotation may carry. Built once, from one extension set, and
never changed — so nothing here needs a cache cleared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, get_args

from . import _linkml

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .ext import Extension

Target = Literal["table", "column"]
TARGETS: tuple[Target, ...] = get_args(Target)


class Vocabulary:
    """Every annotation, enum and structured shape the active profiles declare.

    Tags are full — ``varda:role``, ``acme:cost_center`` — so a tag is
    checked against the extension that owns its prefix rather than against
    the union of everybody's vocabulary.
    """

    def __init__(self, extensions: Iterable[Extension]) -> None:
        """Read every extension's profile, once."""
        #: prefix -> (extension, parsed profile or None)
        self._profiles: dict[str, tuple[Extension, Any]] = {}
        #: target -> [(prefix, annotation class)]
        self._classes: dict[Target, list[tuple[str, Any]]] = {
            t: [] for t in TARGETS
        }
        for ext in extensions:
            view = (
                _linkml.schema(ext.profile) if ext.profile is not None else None
            )
            self._profiles[ext.prefix] = (ext, view)
            if view is None:
                continue
            for cls in view.schema.classes.values():
                target = _linkml.text(cls, f"{ext.prefix}:applies_to")
                if target in TARGETS:
                    self._classes[target].append((ext.prefix, cls))

    @property
    def prefixes(self) -> frozenset[str]:
        """Every active extension's prefix."""
        return frozenset(self._profiles)

    def declared(self, target: Target) -> frozenset[str]:
        """Every annotation tag legal on ``target``."""
        return frozenset(
            f"{prefix}:{attr}"
            for prefix, cls in self._classes[target]
            for attr in cls.attributes or {}
        )

    def governs(self, curie: str) -> Target | None:
        """Name what an annotation class governs, or ``None``.

        The inverse of ``applies_to``: a model element names the class that
        governs it through LinkML's own ``instantiates``, and this resolves
        that against the profile. An unprefixed name resolves to nothing,
        because a bare name says nothing about whose class it is.
        """
        prefix, sep, name = curie.partition(":")
        if not sep:
            return None
        for target in TARGETS:
            for owner, cls in self._classes[target]:
                if owner == prefix and cls.name == name:
                    return target
        return None

    def enum_of(self, target: Target, tag: str) -> str | None:
        """Name the enum an annotation's values must come from, if any."""
        attr = self._attribute(target, tag)
        if attr is None:
            return None
        rng = str(attr.range or "")
        prefix = tag.partition(":")[0]
        return rng if rng in self._enums(prefix) else None

    def shape_of(self, target: Target, tag: str) -> dict[str, str] | None:
        """Name the fields a structured annotation may carry, and their ranges.

        ``None`` when the annotation's range is a scalar or an enum, which is
        the common case.
        """
        attr = self._attribute(target, tag)
        if attr is None:
            return None
        return self.shape(tag.partition(":")[0], str(attr.range or ""))

    def shape(self, prefix: str, name: str) -> dict[str, str] | None:
        """Name the fields of one structured range, and the range of each."""
        _, view = self._profiles.get(prefix, (None, None))
        if view is None:
            return None
        cls = view.schema.classes.get(name)
        if cls is None:
            return None
        return {
            str(field): str(attr.range or "")
            for field, attr in (cls.attributes or {}).items()
        }

    def permitted(self, enum_name: str) -> tuple[str, ...]:
        """Return an enum's permissible values, from its own profile."""
        for _, view in self._profiles.values():
            if view is None or enum_name not in view.schema.enums:
                continue
            enum = view.schema.enums[enum_name]
            return tuple(str(v) for v in (enum.permissible_values or {}))
        return ()

    def profile_filename(self, prefix: str) -> str:
        """Name the file an annotation of this prefix belongs in."""
        ext, _ = self._profiles.get(prefix, (None, None))
        if ext is not None and ext.profile is not None:
            return ext.profile.name
        return f"the {prefix} profile"

    def _attribute(self, target: Target, tag: str) -> Any:
        prefix, _, name = tag.partition(":")
        for owner, cls in self._classes[target]:
            if owner != prefix:
                continue
            attr = (cls.attributes or {}).get(name)
            if attr is not None:
                return attr
        return None

    def _enums(self, prefix: str) -> frozenset[str]:
        _, view = self._profiles.get(prefix, (None, None))
        return frozenset() if view is None else frozenset(view.schema.enums)
