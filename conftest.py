"""Root conftest: make `src/` importable so tests can do `from observers.base import ...`.

Inserting src/ in sys.path here (rather than in a per-test conftest) guarantees
the path adjustment runs before pytest's collector imports any test packages —
important because `tests/unit/observers/__init__.py` would otherwise shadow the
real `src/observers/` package.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
