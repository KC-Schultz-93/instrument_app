"""Reusable themed UI components for instrument_app.

This package exposes a small set of building blocks that subscribe to
:mod:`instrument_app.theme.manager.theme_mgr` and restyle themselves when
themes change.  Widgets are intentionally light-weight so that pages can
compose them without pulling in application logic.
"""

from .primitives import ThemedButton, PillLabel, ValueDisplay, IconDot
from .composites import (
    PressureCard,
    PumpCard,
    PortToolbar,
    AODOPanel,
    AcquisitionPanel,
)
from .plots import TimePressureView
from .collapsible_box import CollapsibleBox
from .toggle_switch import ToggleSwitch
from .param_tile import ParamTile
from .dock_panel import DockHost

__all__ = [
    "ToggleSwitch",
    "ParamTile",
    "DockHost",
    "ThemedButton",
    "PillLabel",
    "ValueDisplay",
    "IconDot",
    "PressureCard",
    "PumpCard",
    "PortToolbar",
    "AODOPanel",
    "AcquisitionPanel",
    "TimePressureView",
    "CollapsibleBox",
]