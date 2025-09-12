"""Deprecated shim for instrument_app.widgets.

Use ``instrument_app.ui`` instead. This package re-exports the public UI
symbols to preserve backward compatibility for one release.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "instrument_app.widgets is deprecated; use instrument_app.ui instead",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export all public UI widgets for compatibility
from instrument_app.ui import *  # noqa: F401,F403
