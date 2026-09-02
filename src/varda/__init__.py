"""Varda — dimensional modeling for LinkML.

A profile of LinkML that adds the vocabulary of dimensional modeling — facts,
dimensions, grain, additivity, slowly-changing dimensions — plus the rules
that check a model against it and the generators that build from it.

Third parties import :mod:`varda.ext`. Everything an extension runs on is
declared there — the extension itself, its generators, the context they are
given, and the :class:`~varda.ext.Finding` and :class:`~varda.ext.RuleSet`
its rules are written with — and everything reachable from there is public
and versioned.

One thing is not settled yet, and saying so is cheaper than being wrong about
it twice: a rule annotated with the type of its argument names
:class:`~varda.model.DimensionalModel`, and the read API hanging off it is
what every rule and generator actually walks. It is documented and typed to
the standard the rest of this package holds, but it has not been declared
public, so it is the one part of the surface that may still move.
"""

from __future__ import annotations

__version__ = "0.3.0"

from .ext import (
    Context,
    Extension,
    ExtensionError,
    Finding,
    Generator,
    RuleError,
    RuleSet,
)
from .model import DimensionalModel

__all__ = [
    "Context",
    "DimensionalModel",
    "Extension",
    "ExtensionError",
    "Finding",
    "Generator",
    "RuleError",
    "RuleSet",
    "__version__",
]
