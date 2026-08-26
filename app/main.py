# instrument_app/app/main.py
from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import Qt, QSettings
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QAction
)
import pyqtgraph as pg

from instrument_app.pages.pressure_page import PressureInterlockPage
from instrument_app.pages.daq_page import DAQPage
from instrument_app.pages.ratemeter_page import RatemeterPage
from instrument_app.services.serial_manager import SerialManager
from instrument_app.services.data_recorder import DataRecorder
from instrument_app.services.daq_channels import DAQChannels

from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme
from instrument_app.app.settings_dialog import SettingsDialog

APP_ORG = "JohnsonLab"
APP_NAME = "NanoInstrumentApp"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Instrument Control")
        self._settings = QSettings(APP_ORG, APP_NAME)

        # theme (apply now, subscribe for changes)
        self._apply_theme(theme_mgr.current)
        theme_mgr.themeChanged.connect(self._apply_theme)

        # services
        self.serial = SerialManager()
        self.recorder = self._make_recorder()
        self.daq_channels = DAQChannels()

        # tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_tabs()

        # mutual exclusion: only one page may hold the PicoScope at a time
        self.daq_channels.daq_busy.connect(self.daq._on_daq_busy)
        self.daq_channels.daq_busy.connect(self.ratemeter._on_daq_busy)

        # menu (Settings only)
        self._build_menu()

        # restore size/last tab
        self._restore_window_state()

    # ---------- UI ----------
    def _build_tabs(self):
        self.pressure = PressureInterlockPage(serial=self.serial, recorder=self.recorder)
        self.daq = DAQPage(self.daq_channels)
        self.ratemeter = RatemeterPage(self.daq_channels)
        self.tabs.addTab(self.pressure, "Pressures / Interlocks")
        self.tabs.addTab(self.daq, "DAQ")
        self.tabs.addTab(self.ratemeter, "Ratemeter")

    def _build_menu(self):
        mbar = self.menuBar()
        m_settings = mbar.addMenu("&Settings")
        act = QAction("Settings…", self)
        act.triggered.connect(self._open_settings)
        m_settings.addAction(act)

    def _open_settings(self):
        SettingsDialog(self).exec()

    # ---------- Theme ----------
    def _apply_theme(self, t: Theme):
        """Apply theme QSS + pyqtgraph colors, then nudge pages to restyle."""
        bg_rule = t.BG_QSS or t.BG
        qss = f"""
            QWidget {{ background:{bg_rule}; color:{t.TXT}; font:10pt 'Segoe UI'; }}

            /* Group boxes — brighter border, visible title */
            QGroupBox {{
                border:1px solid {t.CARD_BORDER};
                border-radius:8px;
                margin-top:10px;
                padding:8px 4px 4px 4px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 4px;
                color: {t.TXT_STRONG};
                font-weight: 600;
            }}

            /* Buttons */
            QPushButton {{
                color:{t.TXT_STRONG}; background:{t.BTN_BG}; border:1px solid {t.BTN_BORDER};
                padding:6px 10px; border-radius:8px;
            }}
            QPushButton:pressed {{ background:{t.BTN_BG_DOWN}; }}
            QPushButton:disabled {{ color:{t.GRAY}; border-color:{t.CARD_BORDER}; }}

            /* Spin boxes and combo boxes — visible border, slightly lighter fill */
            QSpinBox, QDoubleSpinBox, QLineEdit {{
                color:{t.TXT_STRONG}; background:{t.BTN_BG};
                border:1px solid {t.BTN_BORDER};
                border-radius:4px; padding:3px 6px;
                selection-background-color:{t.CARD_BORDER};
            }}
            QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{
                border:1px solid {t.GOOD};
            }}
            QSpinBox::up-button, QSpinBox::down-button,
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
                background:{t.BTN_BG}; border-left:1px solid {t.BTN_BORDER};
                width:16px;
            }}
            QComboBox {{
                color:{t.TXT_STRONG}; background:{t.BTN_BG};
                border:1px solid {t.BTN_BORDER};
                border-radius:4px; padding:3px 6px;
            }}
            QComboBox::drop-down {{ border-left:1px solid {t.BTN_BORDER}; width:20px; }}
            QComboBox QAbstractItemView {{
                background:{t.CARD_BG}; color:{t.TXT}; border:1px solid {t.CARD_BORDER};
                selection-background-color:{t.BTN_BG_DOWN};
            }}

            /* Checkboxes */
            QCheckBox {{ color:{t.TXT}; spacing:6px; }}
            QCheckBox::indicator {{
                width:14px; height:14px;
                border:1px solid {t.BTN_BORDER}; border-radius:3px;
                background:{t.BTN_BG};
            }}
            QCheckBox::indicator:checked {{ background:{t.GOOD}; border-color:{t.GOOD}; }}

            /* Labels — section labels slightly muted, values bright */
            QLabel {{ color:{t.TXT}; }}

            /* Tables */
            QTableWidget {{ background:{t.CARD_BG}; gridline-color:{t.CARD_BORDER}; }}
            QHeaderView::section {{
                background:{t.BTN_BG}; color:{t.TXT_STRONG}; border:1px solid {t.BTN_BORDER};
                padding:4px; font-weight:600;
            }}

            /* Menus */
            QMenuBar {{ background:transparent; color:{t.TXT}; }}
            QMenuBar::item:selected {{ background:{t.BTN_BG_DOWN}; }}
            QMenu {{ background:{t.CARD_BG}; color:{t.TXT}; border:1px solid {t.CARD_BORDER}; }}
            QMenu::item:selected {{ background:{t.BTN_BG}; }}

            /* Scroll bars — subtle but visible */
            QScrollBar:vertical {{
                background:{t.BG}; width:8px; border-radius:4px;
            }}
            QScrollBar::handle:vertical {{
                background:{t.BTN_BORDER}; border-radius:4px; min-height:20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0px; }}

            /* Tabs */
            QTabWidget::pane {{
                border:1px solid {t.CARD_BORDER}; top:-1px; background:{t.CARD_BG};
            }}
            QTabBar::tab {{
                background:{t.BTN_BG}; color:{t.TXT};
                border:1px solid {t.BTN_BORDER};
                padding:6px 10px; margin-right:2px;
                border-top-left-radius:6px; border-top-right-radius:6px;
            }}
            QTabBar::tab:selected {{
                background:{t.CARD_BG}; color:{t.TXT_STRONG};
                border-bottom-color:{t.CARD_BG};
            }}
            QTabBar::tab:!selected:hover {{ background:{t.BTN_BG_DOWN}; }}
        """
        self.setStyleSheet(qss)

        # pyqtgraph global colors
        pg.setConfigOptions(background=t.PLOT_BG, foreground=t.TXT)

        # let pages update any widget-level styles they own
        for page in (getattr(self, "pressure", None), getattr(self, "daq", None), getattr(self, "ratemeter", None)):
            if page and hasattr(page, "_apply_theme_to_self"):
                page._apply_theme_to_self(t)

    # ---------- Settings ----------
    def _restore_window_state(self):
        geo = self._settings.value("main/geometry")
        if geo:
            self.restoreGeometry(geo)
        idx = self._settings.value("main/last_tab")
        if idx is not None:
            try: self.tabs.setCurrentIndex(int(idx))
            except Exception: pass
        self.tabs.currentChanged.connect(
            lambda i: self._settings.setValue("main/last_tab", i)
        )

    def closeEvent(self, ev):
        self._settings.setValue("main/geometry", self.saveGeometry())
        try:
            if hasattr(self.pressure, "close"): self.pressure.close()
            if hasattr(self.daq, "stop_acquisition"): self.daq.stop_acquisition()
            if hasattr(self.ratemeter, "stop_acquisition"): self.ratemeter.stop_acquisition()
        finally:
            super().closeEvent(ev)

    # ---------- Helpers ----------
    def _make_recorder(self):
        logdir = Path.home() / "InstrumentLogs"
        logdir.mkdir(parents=True, exist_ok=True)
        try:
            return DataRecorder(logdir)
        except TypeError:
            return DataRecorder()


def main():
    QApplication.setOrganizationName(APP_ORG)
    QApplication.setApplicationName(APP_NAME)

    app = QApplication(sys.argv)
    win = MainWindow()
    win.resize(1200, 800)
    win.show()
    return app.exec_()
