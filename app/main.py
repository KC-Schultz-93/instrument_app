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
from instrument_app.pages.cdms_page import CDMSPage
from instrument_app.pages.processing_page import ProcessingPage
from instrument_app.services.serial_manager import SerialManager
from instrument_app.services.data_recorder import DataRecorder

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

        # tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_tabs()

        # menu (Settings only)
        self._build_menu()

        # restore size/last tab
        self._restore_window_state()

    # ---------- UI ----------
    def _build_tabs(self):
        self.pressure = PressureInterlockPage(serial=self.serial, recorder=self.recorder)
        self.cdms = CDMSPage()
        self.processing = ProcessingPage()
        self.tabs.addTab(self.pressure, "Pressures / Interlocks")
        self.tabs.addTab(self.cdms, "CDMS")
        self.tabs.addTab(self.processing, "Processing")

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
            QWidget {{ background:{bg_rule}; color:{t.TXT}; }}

            /* Cards, buttons, tables */
            QGroupBox {{ border:1px solid {t.CARD_BORDER}; border-radius:8px; padding:6px; }}
            QPushButton {{
                color:{t.TXT}; background:{t.BTN_BG}; border:1px solid {t.BTN_BORDER};
                padding:6px 10px; border-radius:8px; font:10pt 'Segoe UI';
            }}
            QPushButton:pressed {{ background:{t.BTN_BG_DOWN}; }}

            QTableWidget {{ background:{t.CARD_BG}; gridline-color:{t.CARD_BORDER}; }}
            QHeaderView::section {{
                background:{t.BTN_BG}; color:{t.TXT}; border:1px solid {t.BTN_BORDER};
                padding:4px; font-weight:600;
            }}

            /* Menus */
            QMenuBar {{ background:transparent; color:{t.TXT}; }}
            QMenuBar::item:selected {{ background:{t.BTN_BG_DOWN}; }}
            QMenu {{ background:{t.CARD_BG}; color:{t.TXT}; border:1px solid {t.CARD_BORDER}; }}
            QMenu::item:selected {{ background:{t.BTN_BG}; }}

            /* Tabs */
            QTabWidget::pane {{
                border:1px solid {t.CARD_BORDER};
                top:-1px;
                background:{t.CARD_BG};
            }}
            QTabBar::tab {{
                background:{t.BTN_BG};
                color:{t.TXT};
                border:1px solid {t.BTN_BORDER};
                padding:6px 10px;
                margin-right:2px;
                border-top-left-radius:6px;
                border-top-right-radius:6px;
            }}
            QTabBar::tab:selected {{
                background:{t.CARD_BG};
                color:{t.TXT_STRONG};
                border-bottom-color:{t.CARD_BG};
            }}
            QTabBar::tab:!selected:hover {{ background:{t.BTN_BG_DOWN}; }}
        """
        self.setStyleSheet(qss)

        # pyqtgraph global colors
        pg.setConfigOptions(background=t.PLOT_BG, foreground=t.TXT)

        # let pages update any widget-level styles they own
        for page in (getattr(self, "pressure", None), getattr(self, "cdms", None), getattr(self, "processing", None)):
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
            if hasattr(self.cdms, "close"): self.cdms.close()
            if hasattr(self, "processing") and hasattr(self.processing, "close"): self.processing.close()
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
