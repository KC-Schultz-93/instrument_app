"""Themed on/off switch (painted track + knob)."""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QRectF, QSize, Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QAbstractButton, QWidget

from .mixins import ThemedMixin
from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme


class ToggleSwitch(QAbstractButton, ThemedMixin):
    """Checkable switch. Use the ``clicked`` signal for user actions so that
    programmatic ``setChecked`` calls never re-trigger a handler."""

    def __init__(self, parent: Optional[QWidget] = None):
        QAbstractButton.__init__(self, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(QSize(44, 24))
        self._on = QColor("#2ecc71")
        self._off = QColor("#7f8c8d")
        self._border = QColor("#224050")
        self._knob = QColor("#ffffff")
        ThemedMixin.__init__(self)
        self.apply_theme(theme_mgr.current)

    def sizeHint(self) -> QSize:
        return QSize(44, 24)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(1.0 if self.isEnabled() else 0.4)

        track = QRectF(1, 1, self.width() - 2, self.height() - 2)
        radius = track.height() / 2
        p.setPen(self._border)
        p.setBrush(self._on if self.isChecked() else self._off)
        p.drawRoundedRect(track, radius, radius)

        d = track.height() - 6
        x = track.right() - d - 3 if self.isChecked() else track.left() + 3
        p.setPen(Qt.NoPen)
        p.setBrush(self._knob)
        p.drawEllipse(QRectF(x, track.top() + 3, d, d))

    def apply_theme(self, t: Theme) -> None:
        self._on = QColor(t.GOOD)
        self._off = QColor(t.GRAY)
        self._border = QColor(t.BTN_BORDER)
        self._knob = QColor(t.TXT_STRONG)
        self.update()
