from PyQt5.QtCore import QObject, pyqtSignal, QSettings
from .themes import THEMES, DEFAULT_THEME, Theme

class ThemeManager(QObject):
    themeChanged = pyqtSignal(object)  # emits Theme

    def __init__(self):
        super().__init__()
        self._name = DEFAULT_THEME
        self._theme = THEMES[self._name]
        self._settings = QSettings("JLab", "MRI_Instrument_Control")
        saved = self._settings.value("theme", DEFAULT_THEME, str)
        if saved in THEMES:
            self._name = saved
            self._theme = THEMES[saved]

    @property
    def current(self) -> Theme:
        return self._theme

    @property
    def name(self) -> str:
        return self._name

    def set(self, name: str):
        if name == self._name or name not in THEMES:
            return
        self._name = name
        self._theme = THEMES[name]
        self._settings.setValue("theme", name)
        self.themeChanged.emit(self._theme)

    def available(self):
        return list(THEMES.keys())

theme_mgr = ThemeManager()
