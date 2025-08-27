
"""
Module: instrument_app.theme.style
Purpose: Centralized colors and shared style tokens for a consistent dark UI.

How it fits:
- Used by: pages, widgets, app.main (global stylesheet)

Public API:
- Color constants: BG, CARD_BG, CARD_BORDER, PLOT_FG, BTN_BG, BTN_BG_DOWN, BTN_BORDER, TXT, GOOD, BAD, GRAY

Changelog:
- 2025-08-23 · 0.1.0 · KC · Initial palette sonar stack.

To do:
- Add sonar blip/ping sound effects?
- (Greenish*) 40k theme?

Notes:

"""

# instrument_app/theme/style.py
from .manager import theme_mgr

class _StyleProxy:
    def __getattr__(self, name: str):
        # Forward any attribute lookup (e.g., ``TXT`` or ``BG``) to
        # whatever theme the manager reports as current.
        return getattr(theme_mgr.current, name)

# Public proxy object; acts like a module with color attributes.
style = _StyleProxy()

__all__ = ["style"]
