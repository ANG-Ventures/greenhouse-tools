"""Import shim for floor runners that put tests/ on sys.path without repo root.

Some deterministic floor invocations collect tests by absolute path from outside
this checkout. In that shape, Python can see tests/ but not the repository root,
so imports such as ``from tools import brief_delta`` fail during collection. This
package proxy keeps the tests importing the real tool package without changing
any assertions or test coverage.
"""
from __future__ import annotations

from pathlib import Path
from typing import cast

_REAL_TOOLS = Path(__file__).resolve().parents[2] / "tools"
_PACKAGE_PATH = cast(list[str], __path__)
_PACKAGE_PATH[:] = [str(_REAL_TOOLS)]
