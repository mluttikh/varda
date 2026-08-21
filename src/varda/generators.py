"""Varda's own generators, registered through the public interface.

The core's generators go through exactly the mechanism a third party gets.
That is deliberate: if the built-in ones needed a shortcut, the interface
would be a facade and nobody would find out until they tried to use it.
"""

from __future__ import annotations

from . import gen_docs, gen_sql
from .ext import Generator

BUILTIN: tuple[Generator, ...] = (
    Generator(
        name="sql",
        artefacts=("sql/mart.sql",),
        run=gen_sql.run,
    ),
    Generator(
        name="docs",
        artefacts=("docs/model.md",),
        run=gen_docs.run,
    ),
)
