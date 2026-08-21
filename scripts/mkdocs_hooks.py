"""Local-development convenience for the docs server.

Regenerates the reference pages before every build, so that `mkdocs serve`
reflects a change to a rule's docstring or to the profile without anyone
remembering to re-run the generator.

This is a *convenience*, not the mechanism. `scripts/gen_reference.py` is a
standalone script and remains the source of truth, because the reference pages
have to be buildable by whichever static-site generator this project ends up
on — see `docs/design.md`. A tool that ignores this hook simply needs the
script run first, which is what CI does anyway.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Any

SCRIPT = pathlib.Path(__file__).resolve().parent / "gen_reference.py"


def on_pre_build(config: Any, **kwargs: Any) -> None:  # noqa: ARG001
    """Write the generated reference pages before the site is built.

    Runs in a subprocess rather than importing the generator, and that detail
    is the whole reason this works. `mkdocs serve` is a long-running process:
    by the time a rebuild fires, `varda.rules` is already in `sys.modules`, so
    an in-process call would happily regenerate the pages from the *stale*
    module and report success. Editing a rule docstring would rebuild the site
    and change nothing, which is worse than not reloading at all.

    A fresh interpreter imports the current source. It costs a process spawn
    per rebuild, which is invisible next to the site build itself.
    """
    subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)], check=True, capture_output=True
    )
