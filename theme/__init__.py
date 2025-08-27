# instrument_app/theme/__init__.py
from .manager import theme_mgr
from .style import style  # dynamic proxy -> current theme
from .themes import Theme, THEMES, DEFAULT_THEME

__all__ = ["theme_mgr", "style", "Theme", "THEMES", "DEFAULT_THEME"]
