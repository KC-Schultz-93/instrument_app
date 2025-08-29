from __future__ import annotations
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QCheckBox
)
from instrument_app.theme.manager import theme_mgr

#Depends on theme_mgr which depends on themes

class SettingsDialog(QDialog):
    """
    Minimal settings: pick theme and make it the startup default.
    theme_mgr already persists the selected theme via QSettings.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)

        v = QVBoxLayout(self)

        # Theme picker
        row = QHBoxLayout()
        row.addWidget(QLabel("Theme:"))
        self.cb_theme = QComboBox()
        self.cb_theme.addItems(theme_mgr.available())
        self.cb_theme.setCurrentText(theme_mgr.name)
        row.addWidget(self.cb_theme, 1)
        v.addLayout(row)

        # Optional: “apply immediately” checkbox
        self.chk_apply = QCheckBox("Apply changes immediately")
        self.chk_apply.setChecked(True)
        v.addWidget(self.chk_apply)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_apply = QPushButton("Apply")
        self.btn_close = QPushButton("Close")
        btn_row.addWidget(self.btn_apply)
        btn_row.addWidget(self.btn_close)
        v.addLayout(btn_row)

        # Wire up
        self.btn_apply.clicked.connect(self._apply_clicked)
        self.btn_close.clicked.connect(self.accept)

        # Apply-on-change (optional)
        self.cb_theme.currentTextChanged.connect(
            lambda name: self._apply_clicked() if self.chk_apply.isChecked() else None
        )

    def _apply_clicked(self):
        # Sets + persists to QSettings; theme_mgr emits themeChanged
        theme_mgr.set(self.cb_theme.currentText())
