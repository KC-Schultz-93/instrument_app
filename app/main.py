
"""
Module: instrument_app.app.main
Purpose: Application shell that builds the QMainWindow and tabs, wires shared services,
         and starts the Qt event loop via main().

How it fits:
- Depends on: instrument_app.services.serial_manager, data_recorder
              instrument_app.pages.pressure_page, cdms_page
              instrument_app.theme.style
- Used by:    instrument_app.__main__ (python -m instrument_app), direct script runs

Public API:
- class MainWindow(QMainWindow)
- def main(argv: list[str]|None = None) -> int

Changelog:
- 2025-08-23 · 0.1.0 · KC · Modular app shell with Pressures/Interlocks + CDMS tab.
"""

# ment_app/app/main.py
import sys
import argparse
from PyQt5.QtWidgets import QApplication, QMainWindow, QTabWidget
from instrument_app.theme import style
from instrument_app.services.serial_manager import SerialManager
from instrument_app.services.data_recorder import DataRecorder
from instrument_app.pages.pressure_page import PressureInterlockPage
from instrument_app.pages.cdms_page import CDMSPage

class MainWindow(QMainWindow):
    def __init__(self, *, data_dir="data"):
        super().__init__()
        self.setWindowTitle("Instrument Control")
        self.resize(1280, 800)

        # Tabs
        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        # Shared services
        self.serial = SerialManager()
        self.rec    = DataRecorder(root=data_dir)

        # Pages
        self.pressure = PressureInterlockPage(self.serial, self.rec)
        self.cdms     = CDMSPage()

        tabs.addTab(self.pressure, "Pressures / Interlocks")
        tabs.addTab(self.cdms, "CDMS")

        # (Optional) local stylesheet for tabs/background
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{ background: {style.BG}; border: 0px; }}
            QWidget {{ background: {style.BG}; color: {style.TXT}; }}
            QTabBar::tab {{
                background: {style.BTN_BG}; color: {style.TXT};
                border: 1px solid {style.BTN_BORDER};
                padding: 6px 10px; margin-right: 6px;
                border-top-left-radius: 8px; border-top-right-radius: 8px;
            }}
            QTabBar::tab:selected {{ background: {style.BTN_BG_DOWN}; }}
        """)

    def closeEvent(self, ev):
        try:
            self.serial.disconnect()
        finally:
            super().closeEvent(ev)

def build_arg_parser():
    p = argparse.ArgumentParser(prog="instrument_app",
                                description="Instrument control GUI")
    p.add_argument("--data-dir", default="data",
                   help="where CSV output is written (default: %(default)s)")
    # Add more flags later, e.g. --port COM4, --baud 115200, --style fusion
    return p

def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns process exit code."""
    args = build_arg_parser().parse_args(argv)

    # Qt app
    app = QApplication(sys.argv)  # keep sys.argv so Qt picks up DPI flags etc.

    # (Optional) global stylesheet; you can also leave this in the page
    app.setStyleSheet(f"""
        QMainWindow {{ background: {style.BG}; color: {style.TXT}; }}
        QToolTip {{ color: {style.TXT}; background: {style.CARD_BG}; }}
    """)

    win = MainWindow(data_dir=args.data_dir)
    win.show()
    return app.exec_()

# Allow direct python file execution too (useful for tests)
if __name__ == "__main__":
    sys.exit(main())
