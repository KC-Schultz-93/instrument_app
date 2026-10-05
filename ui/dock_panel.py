"""Docked option panel host: shows one panel at a time, hidden when closed."""
from __future__ import annotations

from typing import Dict, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame, QLabel, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from .mixins import ThemedMixin
from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme

DOCK_WIDTH = 320


class DockHost(QFrame, ThemedMixin):
    """Header title + scrollable stack of panels keyed by tile id."""

    changed = pyqtSignal(object)  # key (str) or None when closed

    def __init__(self, parent: Optional[QWidget] = None):
        QFrame.__init__(self, parent)
        self.setObjectName("DockHost")
        self.setFixedWidth(DOCK_WIDTH)
        self._titles: Dict[str, str] = {}
        self._index: Dict[str, int] = {}
        self._key: Optional[str] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        self._header = QLabel("")
        lay.addWidget(self._header)

        self._stack = QStackedWidget()
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidget(self._stack)
        lay.addWidget(self._scroll, 1)

        self.setVisible(False)
        ThemedMixin.__init__(self)
        self.apply_theme(theme_mgr.current)

    @property
    def current_key(self) -> Optional[str]:
        return self._key

    def add_panel(self, key: str, title: str, panel: QWidget) -> None:
        self._titles[key] = title
        panel.setObjectName("DockPanel")
        self._index[key] = self._stack.addWidget(panel)
        self.apply_theme(theme_mgr.current)

    def open(self, key: str) -> None:
        if key not in self._index:
            raise KeyError(key)
        self._stack.setCurrentIndex(self._index[key])
        self._header.setText(self._titles[key])
        self.setVisible(True)
        if self._key != key:
            self._key = key
            self.changed.emit(key)

    def close(self) -> None:  # type: ignore[override]
        self.setVisible(False)
        if self._key is not None:
            self._key = None
            self.changed.emit(None)

    def toggle(self, key: str) -> None:
        if self._key == key:
            self.close()
        else:
            self.open(key)

    def apply_theme(self, t: Theme) -> None:
        self.setStyleSheet(
            f"#DockHost{{background:{t.CARD_BG}; border:1px solid {t.CARD_BORDER}; border-radius:8px;}}"
            f"#DockHost QScrollArea, #DockHost QStackedWidget{{background:{t.CARD_BG};}}"
        )
        self._header.setStyleSheet(
            f"background:transparent; color:{t.TXT_STRONG}; font:bold 11pt 'Segoe UI'; padding:2px 4px;"
        )
        # Panels are plain widgets on the app-wide gradient; give each an
        # explicit card background so they match the dock.
        for i in range(self._stack.count()):
            w = self._stack.widget(i)
            w.setStyleSheet(
                f"QWidget#DockPanel{{background:{t.CARD_BG};}}"
                f"QLabel{{background:transparent; color:{t.TXT};}}"
                f"QCheckBox{{background:transparent; color:{t.TXT};}}"
            )
