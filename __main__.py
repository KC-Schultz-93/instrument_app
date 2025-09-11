"""Module entry-point for `python -m instrument_app` and local runs.

This adjusts `sys.path` when executed from inside the repository folder
so that `import instrument_app` resolves correctly without installation.
"""
from pathlib import Path
import sys

# If running from the package directory (repo root), add its parent so
# `import instrument_app` resolves to this folder instead of looking for
# a nested `instrument_app/instrument_app`.
_here = Path(__file__).resolve().parent
_parent = _here.parent
if _here.name == "instrument_app" and _parent.as_posix() not in map(str, sys.path):
    sys.path.insert(0, str(_parent))

from instrument_app.app.main import main

if __name__ == "__main__":
    raise SystemExit(main())

