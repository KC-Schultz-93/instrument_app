import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from PyQt5.QtWidgets import QApplication, QMainWindow, QTabWidget
from PyQt5.QtGui import QPalette, QColor

from Pages.pressure_page import PressurePage
from Pages.daq_page import DAQPage
from Services.Channels import AppChannels
from UI.theme import theme_mgr


def apply_theme(app, theme):
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(theme.BG))
    palette.setColor(QPalette.WindowText, QColor(theme.TXT))
    palette.setColor(QPalette.Base, QColor(theme.CARD_BG))
    palette.setColor(QPalette.AlternateBase, QColor(theme.CARD_BG))
    palette.setColor(QPalette.Text, QColor(theme.TXT))
    palette.setColor(QPalette.Button, QColor(theme.BTN_BG))
    palette.setColor(QPalette.ButtonText, QColor(theme.TXT))
    palette.setColor(QPalette.Highlight, QColor(theme.GOOD))
    palette.setColor(QPalette.HighlightedText, QColor(theme.TXT_STRONG))
    app.setPalette(palette)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vacuum System Control")
        self.resize(1400, 800)

        self.channels = AppChannels()

        tabs = QTabWidget()
        self.pressure_page = PressurePage(self.channels)
        self.daq_page = DAQPage(self.channels)
        tabs.addTab(self.pressure_page, "Pressures / Interlocks")
        tabs.addTab(self.daq_page, "CDMS")
        self.setCentralWidget(tabs)

    def closeEvent(self, event) -> None:
        """Ensure the DAQ worker thread is stopped cleanly before exit."""
        self.daq_page.stop_acquisition()
        super().closeEvent(event)


def main():
    import traceback

    # Add exception handler to catch crashes
    def exception_handler(exc_type, exc_value, exc_traceback):
        error_msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        with open('crash_log.txt', 'a') as f:
            f.write(f"\n{'='*80}\n")
            f.write(f"CRASH at {__import__('datetime').datetime.now()}\n")
            f.write(error_msg)
        print(f"FATAL ERROR: {error_msg}")
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = exception_handler

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    apply_theme(app, theme_mgr.current)
    theme_mgr.themeChanged.connect(lambda theme: apply_theme(app, theme))

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
