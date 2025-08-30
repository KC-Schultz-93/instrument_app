from PyQt5.QtCore import QObject, pyqtSignal, QSettings
from .themes import THEMES, DEFAULT_THEME, Theme

class ThemeManager(QObject):
    themeChanged = pyqtSignal(object)  # emits Theme

    def __init__(self):
        super().__init__()
        self._settings = QSettings("KCLab", "InstrumentApp")
        name = self._settings.value("theme", DEFAULT_THEME, str)
        if name not in THEMES:
            name = DEFAULT_THEME
        self._name = name
        self._theme = THEMES[name]

    @property
    def current(self) -> Theme:
        return self._theme

    @property
    def name(self) -> str:
        return self._name

    def available(self):
        return list(THEMES.keys())

    def set(self, name: str):
        if name == self._name or name not in THEMES:
            return
        self._name = name
        self._theme = THEMES[name]
        self._settings.setValue("theme", name)
        self.themeChanged.emit(self._theme)

theme_mgr = ThemeManager()

