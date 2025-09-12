"""Deprecated alias module.

This module preserves the old import path
``instrument_app.widgets.time_pressure_plot.TimePressurePlot`` by aliasing
to the new widget in ``instrument_app.ui.plots``.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "instrument_app.widgets.time_pressure_plot is deprecated; "
    "use instrument_app.ui.plots.TimePressureView",
    DeprecationWarning,
    stacklevel=2,
)

from instrument_app.ui import TimePressureView as TimePressurePlot  # noqa: F401

