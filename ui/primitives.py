"""Basic themed widgets used across the application."""
from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtWidgets import QPushButton, QLabel
from PyQt5.QtCore import Qt

from .mixins import ThemedMixin
from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme


class ThemedButton(ThemedMixin, QPushButton):
    """Push button that follows the current theme."""

    def __init__(self, text: str = "", *, height: Optional[int] = None, parent=None):
        super().__init__(text, parent)
        if height is not None:
            self.setFixedHeight(int(height))

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        self.setStyleSheet(
            f"QPushButton{{color:{t.TXT}; background:{t.BTN_BG}; border:1px solid {t.BTN_BORDER};"
            f"padding:6px 10px; border-radius:8px; font:10pt 'Segoe UI';}}"
            f"QPushButton:pressed{{background:{t.BTN_BG_DOWN};}}"
        )


class PillLabel(ThemedMixin, QLabel):
    """Rounded label whose colors are derived from the theme."""

    def __init__(
        self,
        text: str = "",
        *,
        bg_role: Callable[[Theme], str] | str | None = None,
        fg_role: Callable[[Theme], str] | str | None = None,
        parent=None,
    ):
        self._bg_role = bg_role or (lambda t: t.CARD_BG)
        self._fg_role = fg_role or (lambda t: t.TXT)
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)

    def set_roles(
        self,
        bg_role: Callable[[Theme], str] | str | None = None,
        fg_role: Callable[[Theme], str] | str | None = None,
    ) -> None:
        if bg_role is not None:
            self._bg_role = bg_role
        if fg_role is not None:
            self._fg_role = fg_role
        self.apply_theme(theme_mgr.current)

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        bg = self._bg_role(t) if callable(self._bg_role) else self._bg_role
        fg = self._fg_role(t) if callable(self._fg_role) else self._fg_role
        self.setStyleSheet(
            f"QLabel{{background:{bg}; color:{fg}; padding:4px 8px; border-radius:8px; font:10pt 'Segoe UI';}}"
        )


class ValueDisplay(ThemedMixin, QLabel):
    """Monospaced value display used inside pressure cards."""

    def __init__(self, text: str = "--  TORR", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        self.setStyleSheet(
            f"font:20pt 'Consolas'; background:#000; color:{t.BAD}; border-radius:6px; padding:4px;"
        )


class IconDot(ThemedMixin, QLabel):
    """Small circular status indicator."""

    def __init__(self, diameter: int = 14, parent=None):
        self._diameter = int(diameter)
        self._color: Optional[str] = None
        super().__init__("", parent)
        self.setFixedSize(self._diameter, self._diameter)

    def set_color(self, color: Optional[str]) -> None:
        """Set explicit *color* for the dot (None -> theme gray)."""
        self._color = color
        self.apply_theme(theme_mgr.current)

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        col = self._color or t.GRAY
        radius = self._diameter // 2
        self.setStyleSheet(
            f"background:{col}; border-radius:{radius}px; border:1px solid {t.CARD_BORDER};"
        )