# instrument_app/app/main.py
from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import Qt, QSettings
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QAction, QActionGroup, QMessageBox
)
import pyqtgraph as pg

# settings / dialogs
from instrument_app.app.settings_dialog import SettingsDialog

# pages / services
from instrument_app.pages.pressure_page import PressureInterlockPage
from instrument_app.pages.cdms_page import CDMSPage
from instrument_app.services.serial_manager import SerialManager
from instrument_app.services.data_recorder import DataRecorder

# theming
from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme


APP_ORG = "JLab"
APP_NAME = "MRI_Instrument_Control"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Instrument Control")

        # settings (window geometry, last tab, theme is handled by ThemeManager)
        self._settings = QSettings(APP_ORG, APP_NAME)

        # apply theme now, subscribe for live changes
        self._apply_theme(theme_mgr.current)
        theme_mgr.themeChanged.connect(self._apply_theme)

        # services (serial + recorder)
        self.serial = SerialManager()
        self.recorder = self._make_recorder()

        # tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_tabs()

        # menubar
        self._build_menu()

        # restore window state
        self._restore_window_state()

    # ------------ UI construction ------------

    def _build_tabs(self):
        self.pressure = PressureInterlockPage(serial=self.serial, recorder=self.recorder)
        self.cdms = CDMSPage()  # pass daq when ready: CDMSPage(daq=self.ni)

        self.tabs.addTab(self.pressure, "Pressures / Interlocks")
        self.tabs.addTab(self.cdms, "CDMS")

    def _build_menu(self):
        mbar = self.menuBar()

        # Theme menu (as before)...
        m_theme = mbar.addMenu("&Theme")
        group = QActionGroup(self); group.setExclusive(True)
        for name in theme_mgr.available():
            act = QAction(name, self, checkable=True, checked=(name == theme_mgr.name))
            act.triggered.connect(lambda _=False, n=name: theme_mgr.set(n))
            group.addAction(act); m_theme.addAction(act)

        # Settings
        m_app = mbar.addMenu("&Settings")
        act_settings = QAction("Settings…", self)
        act_settings.triggered.connect(self._open_settings)
        m_app.addAction(act_settings)

        # Help (as before)...
        m_help = mbar.addMenu("&Help")
        about = QAction("About…", self)
        about.triggered.connect(self._show_about)
        m_help.addAction(about)

    def _open_settings(self):
        SettingsDialog(self).exec()

    # ------------ Theme hook ------------

    def _apply_theme(self, t: Theme):
        """Apply the selected Theme to the whole window."""
        # Gradients for big surfaces; solid for everything else
        bg_rule = t.BG
        qss = f"""
            QWidget {{ background:{bg_rule}; color:{t.TXT}; }}
            QTabWidget::pane {{position: absolute;}}
            QTabWidget::tab-bar {{left: 15px;}}
            QTabBar::tab {{background:{t.BTN_BG}; color:{t.TXT};
                            padding: 6px 12px;
                            border-top-left-radius: 8px;
                            border-top-right-radius: 8px; }}
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
        """
        self.setStyleSheet(qss)

        # pyqtgraph global colors (per-theme solid)
        pg.setConfigOptions(background=t.PLOT_BG, foreground=t.PLOT_FG)

        # Let pages re-apply any per-widget styles they own (optional)
        for page in (getattr(self, "pressure", None), getattr(self, "cdms", None)):
            if page and hasattr(page, "_apply_theme_to_self"):
                page._apply_theme_to_self(t)  # noqa: SLF001 (private helper by convention)

    # ------------ Settings ------------

    def _restore_window_state(self):
        geo = self._settings.value("main/geometry")
        if geo:
            self.restoreGeometry(geo)
        idx = self._settings.value("main/last_tab")
        if idx is not None:
            try:
                self.tabs.setCurrentIndex(int(idx))
            except Exception:
                pass
        # remember tab on change
        self.tabs.currentChanged.connect(
            lambda i: self._settings.setValue("main/last_tab", i)
        )

    def closeEvent(self, ev):
        self._settings.setValue("main/geometry", self.saveGeometry())
        try:
            # give pages a chance to stop threads cleanly
            if hasattr(self.pressure, "close"):
                self.pressure.close()
            if hasattr(self.cdms, "close"):
                self.cdms.close()
        finally:
            super().closeEvent(ev)

    # ------------ Helpers ------------

    def _make_recorder(self):
        """Construct DataRecorder with a sensible default path, regardless of ctor signature."""
        logdir = Path.home() / "InstrumentLogs"
        logdir.mkdir(parents=True, exist_ok=True)
        try:
            return DataRecorder(logdir)
        except TypeError:
            # your DataRecorder may not need a path
            return DataRecorder()

    def _show_about(self):
        QMessageBox.information(
            self,
            "About",
            "InstrumentApp\n\nPressures/Interlocks + CDMS acquisition and analysis.\n"
            "Themeable UI with live switching.",
        )


def main():
    # QSettings keys used by ThemeManager require these to be set early
    QApplication.setOrganizationName(APP_ORG)
    QApplication.setApplicationName(APP_NAME)

    app = QApplication(sys.argv)
    win = MainWindow()
    win.resize(1200, 800)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
