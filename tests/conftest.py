"""Shared fixtures.

The extension fixture lives in ``tests/fixtures``, which is not on the path by
default. Adding it here rather than in each test keeps the import in one
place, and mirrors what installing an extension would do.
"""

from __future__ import annotations

import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parents[1] / "examples"

if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))
