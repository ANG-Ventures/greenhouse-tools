"""Test package bootstrap for floor runners launched outside the repo root."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
root_s = str(ROOT)
if root_s not in sys.path:
    sys.path.insert(0, root_s)
