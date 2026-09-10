"""Collapsible section container — lightweight alternative to QGroupBox."""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QToolButton, QSizePolicy

from .mixins import ThemedMixin
from instrument_app.theme.themes import Theme


class CollapsibleBox(QWidget, ThemedMixin):
    """Titled section with a checkable header that shows/hides its content.

    No animation: toggling just calls ``content.setVisible(checked)``, which
    is enough since Qt layouts exclude hidden widgets from size calculations.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, title: str = "", parent: Optional[QWidget] = None):
        QWidget.__init__(self, parent)
        self._title = title

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._toggle_btn = QToolButton(self)
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setChecked(True)
        self._toggle_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._toggle_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._toggle_btn.toggled.connect(self._on_toggled)
        self._update_button_text(True)

        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)

        outer.addWidget(self._toggle_btn)
        outer.addWidget(self.content)

        ThemedMixin.__init__(self)

    def isExpanded(self) -> bool:
        return self._toggle_btn.isChecked()

    def setExpanded(self, expanded: bool) -> None:
        """Programmatic restore (e.g. from QSettings) — does not emit toggled."""
        self._toggle_btn.blockSignals(True)
        self._toggle_btn.setChecked(expanded)
        self._toggle_btn.blockSignals(False)
        self._apply_expanded(expanded)

    def _on_toggled(self, checked: bool) -> None:
        self._apply_expanded(checked)
        self.toggled.emit(checked)

    def _apply_expanded(self, checked: bool) -> None:
        self._update_button_text(checked)
        self.content.setVisible(checked)

    def _update_button_text(self, expanded: bool) -> None:
        arrow = "▼" if expanded else "▶"
        self._toggle_btn.setText(f"{arrow}  {self._title}")

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        self._toggle_btn.setStyleSheet(
            f"QToolButton{{color:{t.TXT}; background:{t.BTN_BG}; border:1px solid {t.BTN_BORDER};"
            f"padding:6px 10px; border-radius:8px; font:10pt 'Segoe UI'; text-align:left;}}"
            f"QToolButton:checked{{background:{t.BTN_BG_DOWN};}}"
        )
        self.content.setStyleSheet(
            f"background:{t.CARD_BG}; border:1px solid {t.CARD_BORDER}; border-radius:8px;"
        )
