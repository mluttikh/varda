"""Annotation access.

LinkML stores annotations as ``JsonObj`` in some code paths and plain dicts in
others, and the two do not share an interface. Every read of an annotation in
this package goes through :func:`anns`, so that inconsistency is handled in
exactly one place.

Reads are namespaced. A :class:`Reader` is bound to one prefix and sees only
that prefix's annotations, which is what lets Varda and a third-party
extension annotate the same class without either guessing at the other's
vocabulary. Varda's own reader is :data:`varda`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from jsonasobj2 import items as _ja_items


def anns(obj: Any) -> dict[str, Any]:
    """Return an object's annotations as a plain ``{tag: value}`` dict."""
    declared = getattr(obj, "annotations", None)
    if not declared:
        return {}
    out: dict[str, Any] = {}
    for tag, ann in _ja_items(declared):
        value = (
            ann.get("value")
            if isinstance(ann, dict)
            else getattr(ann, "value", ann)
        )
        out[str(tag)] = value
    return out


@dataclass(frozen=True)
class Reader:
    """Reads one namespace's annotations off a LinkML object.

    The prefix *is* the namespace, and the match is exact: a reader for
    ``acme`` never sees ``varda:role``, and Varda's reader never sees
    ``acme:cost_center``. Reads are deliberately strict about the prefix — an
    unprefixed ``role:`` annotation is nobody's, not everybody's, and
    accepting it as Varda's is how one party's vocabulary silently consumes
    another's.
    """

    prefix: str

    @property
    def tag(self) -> str:
        """The prefix with its colon, as it appears on an annotation."""
        return f"{self.prefix}:"

    def raw(self, obj: Any, key: str) -> Any:
        """Read one annotation, with or without the prefix on ``key``.

        Returns the value as LinkML stored it. Only the converters below
        should need this; everything else wants :meth:`get`.
        """
        full = key if key.startswith(self.tag) else self.tag + key
        return anns(obj).get(full)

    def get(self, obj: Any, key: str) -> str | None:
        """Read one annotation as a string.

        The coercion is the point. A value arrives as whatever the YAML parser
        made of it — ``16`` is an int, ``true`` is a bool — and every caller
        here wants a string. Doing it once means the accessors in
        :mod:`varda.model` can promise ``str | None`` and be telling the
        truth, rather than promising it while handing back ``Any``.
        """
        value = self.raw(obj, key)
        return None if value is None else str(value)

    def get_list(self, obj: Any, key: str) -> tuple[str, ...]:
        """Read one annotation as a tuple of strings.

        A YAML list arrives as a list and a bare scalar arrives as a scalar,
        and both are legitimate ways to write a one-element annotation. The
        scalar is widened rather than rejected, because `grain: order_id` is
        what someone writes for a single-column grain and refusing it would
        be pedantry with no safety behind it.
        """
        value = self.raw(obj, key)
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            return (str(value),)
        if isinstance(value, Iterable):
            return tuple(str(v) for v in value)
        return (str(value),)

    def present(self, obj: Any) -> bool:
        """Flag whether the object carries any annotation in this namespace."""
        return any(k.startswith(self.tag) for k in anns(obj))

    def keys(self, obj: Any) -> list[str]:
        """List this namespace's annotation names, prefix stripped."""
        n = len(self.tag)
        return sorted(k[n:] for k in anns(obj) if k.startswith(self.tag))


#: Varda's own reader.
varda = Reader("varda")

get = varda.get
get_list = varda.get_list


def is_model_object(obj: Any) -> bool:
    """Flag whether an object participates in the dimensional model.

    Deliberately *not* prefix-aware. A class is part of the model because
    Varda says it is, and a class carrying only ``acme:`` annotations is a
    class an extension has decorated — not one it has enrolled. Making this
    prefix-aware would let an extension pull arbitrary classes into the model
    by annotating them, which is exactly the authority extensions do not have.
    """
    return varda.present(obj)
