"""Mixin utilities for themed widgets."""
from __future__ import annotations

from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme


class ThemedMixin:
    """Mixin that updates widgets when the global theme changes.

    Subclasses must implement :meth:`apply_theme` and call ``super().__init__``
    during construction so the mixin can subscribe to theme changes.
    """

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        theme_mgr.themeChanged.connect(self.apply_theme)  # type: ignore[arg-type]
        self.apply_theme(theme_mgr.current)

    def apply_theme(self, theme: Theme) -> None:  # pragma: no cover - to be overridden
        """Apply colors/fonts for *theme*.

        Subclasses override this method to set widget-specific style sheets or
        palette properties.  ``Theme`` is a frozen dataclass with color tokens
        defined in :mod:`instrument_app.theme.themes`.
        """
        raise NotImplementedError