"""Themed card container (explicit background so it matches on every theme)."""
from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import QFrame, QWidget

from .mixins import ThemedMixin
from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme


class CardFrame(QFrame, ThemedMixin):
    def __init__(self, parent: Optional[QWidget] = None):
        QFrame.__init__(self, parent)
        self.setObjectName("CardFrame")
        ThemedMixin.__init__(self)
        self.apply_theme(theme_mgr.current)

    def apply_theme(self, t: Theme) -> None:
        self.setStyleSheet(
            f"#CardFrame{{background:{t.CARD_BG}; border:1px solid {t.CARD_BORDER}; border-radius:8px;}}"
            f"#CardFrame QLabel{{background:transparent;}}"
        )
