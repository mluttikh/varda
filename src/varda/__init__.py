"""Varda — dimensional modeling for LinkML.

A profile of LinkML that adds the vocabulary of dimensional modeling — facts,
dimensions, grain, additivity, slowly-changing dimensions — plus the rules
that check a model against it and the generators that build from it.

Two public surfaces, for two audiences:

- :mod:`varda.api` is Varda as a library — load a model, check it, generate
  from it, write what was generated. The command line is a thin layer over
  it.
- :mod:`varda.ext` is what an extension is written against — the model a
  rule walks, the analysis Varda reads into it, and the types a rule and a
  generator are made of. An extension imports that module and nothing else.

Everything reachable from either is public and versioned.
"""

from __future__ import annotations

__version__ = "0.3.0"

from . import api
from .api import check, generate, load, write
from .ext import (
    Context,
    Extension,
    ExtensionError,
    Finding,
    Generator,
    Issue,
    Model,
    Options,
    Report,
    RuleError,
    RuleSet,
    Severity,
)
from .registry import Registry

__all__ = [
    "Context",
    "Extension",
    "ExtensionError",
    "Finding",
    "Generator",
    "Issue",
    "Model",
    "Options",
    "Registry",
    "Report",
    "RuleError",
    "RuleSet",
    "Severity",
    "__version__",
    "api",
    "check",
    "generate",
    "load",
    "write",
]
