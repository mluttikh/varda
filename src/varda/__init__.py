"""Varda — dimensional modelling for LinkML.

A profile of LinkML that adds the vocabulary of dimensional modelling — facts,
dimensions, grain, additivity, slowly-changing dimensions — plus the rules
that check a model against it and the generators that build from it.

Third parties import :mod:`varda.ext` and nothing else. Everything reachable
from there is public and versioned; everything else is internal and moves
without notice.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .ext import Context, Extension, ExtensionError, Generator
from .model import DimensionalModel
from .rules import Finding, RuleSet

__all__ = [
    "Context",
    "DimensionalModel",
    "Extension",
    "ExtensionError",
    "Finding",
    "Generator",
    "RuleSet",
    "__version__",
]
