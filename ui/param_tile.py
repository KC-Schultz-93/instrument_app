"""Compact tile showing one setting's current value (PicoScope-style)."""
from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .mixins import ThemedMixin
from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme


class ParamTile(QFrame, ThemedMixin):
    """Title, primary value, optional secondary lines and optional -/+ buttons.

    ``clicked`` fires for a click on the tile body (used to open/close a dock
    panel); the -/+ buttons emit ``step_down`` / ``step_up`` instead.
    """

    clicked = pyqtSignal()
    step_down = pyqtSignal()
    step_up = pyqtSignal()

    def __init__(self, title: str, stepper: bool = False, parent: Optional[QWidget] = None):
        QFrame.__init__(self, parent)
        self.setObjectName("ParamTile")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._open = False
        self._warn = False
        self._theme: Theme = theme_mgr.current
        self.setMinimumHeight(72)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(1)

        self._title = QLabel(title)
        outer.addWidget(self._title)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_minus = QToolButton()
        self.btn_minus.setText("−")
        self.btn_minus.clicked.connect(self.step_down)
        self.btn_plus = QToolButton()
        self.btn_plus.setText("+")
        self.btn_plus.clicked.connect(self.step_up)
        self._primary = QLabel("")
        self._primary.setAlignment(Qt.AlignCenter)
        self._primary.setWordWrap(True)
        if stepper:
            row.addWidget(self.btn_minus)
        row.addWidget(self._primary, 1)
        if stepper:
            row.addWidget(self.btn_plus)
        else:
            self.btn_minus.hide()
            self.btn_plus.hide()
        outer.addLayout(row)

        self._secondary: List[QLabel] = []
        self._secondary_box = QVBoxLayout()
        self._secondary_box.setSpacing(0)
        outer.addLayout(self._secondary_box)

        for lbl in (self._title, self._primary):
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

        ThemedMixin.__init__(self)
        self.apply_theme(self._theme)

    # -- public API ------------------------------------------------------

    def set_values(self, primary: str, secondary: Optional[List[str]] = None, warn: bool = False) -> None:
        self._primary.setText(("⚠ " if warn else "") + primary)
        secondary = secondary or []
        while len(self._secondary) < len(secondary):
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            self._secondary_box.addWidget(lbl)
            self._secondary.append(lbl)
        for i, lbl in enumerate(self._secondary):
            lbl.setVisible(i < len(secondary))
            if i < len(secondary):
                lbl.setText(secondary[i])
        self._warn = warn
        self.apply_theme(self._theme)

    def primary_text(self) -> str:
        return self._primary.text()

    def set_open(self, is_open: bool) -> None:
        self._open = is_open
        self.apply_theme(self._theme)

    def is_open(self) -> bool:
        return self._open

    # -- events ----------------------------------------------------------

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    # -- theming ---------------------------------------------------------

    def apply_theme(self, t: Theme) -> None:
        self._theme = t
        bg = t.BTN_BG_DOWN if self._open else t.BTN_BG
        border = t.GOOD if self._open else t.BTN_BORDER
        self.setStyleSheet(
            f"#ParamTile{{background:{bg}; border:1px solid {border}; border-radius:8px;}}"
            f"#ParamTile QLabel{{background:transparent; color:{t.TXT};}}"
            f"#ParamTile QToolButton{{background:{t.CARD_BG}; color:{t.TXT};"
            f" border:1px solid {t.BTN_BORDER}; border-radius:6px; min-width:22px; min-height:22px;}}"
            f"#ParamTile QToolButton:pressed{{background:{t.BTN_BG_DOWN};}}"
        )
        self._title.setStyleSheet(f"color:{t.TXT_MUTED}; font:8pt 'Segoe UI';")
        primary_color = t.BAD if self._warn else t.TXT_STRONG
        self._primary.setStyleSheet(f"color:{primary_color}; font:bold 11pt 'Segoe UI';")
        for lbl in self._secondary:
            lbl.setStyleSheet(f"color:{t.TXT_MUTED}; font:8pt 'Segoe UI';")
