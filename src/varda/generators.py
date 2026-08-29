"""Varda's own generators, registered through the public interface.

The core's generators go through exactly the mechanism a third party gets.
That is deliberate: if the built-in ones needed a shortcut, the interface
would be a facade and nobody would find out until they tried to use it.
"""

from __future__ import annotations

from . import gen_assertions, gen_docs, gen_sql
from .ext import Generator

BUILTIN: tuple[Generator, ...] = (
    Generator(
        name="sql",
        artifacts=("sql/mart.sql",),
        run=gen_sql.run,
    ),
    # Registered unconditionally rather than only under a weaker
    # `--constraints`. A generator whose output appears and disappears with
    # a flag is one nobody wires into a pipeline, and this file is worth
    # running against an enforcing database too: it is the same claims,
    # checked when the data changes rather than on every write, and it is
    # the only form of them a warehouse that cannot enforce keys can run.
    Generator(
        name="assertions",
        artifacts=("sql/assertions.sql",),
        run=gen_assertions.run,
    ),
    Generator(
        name="docs",
        artifacts=("docs/model.md",),
        run=gen_docs.run,
    ),
)
