
"""
Module: instrument_app.theme.style
Purpose: Centralized colors and shared style tokens for a consistent dark UI.

How it fits:
- Used by: pages, widgets, app.main (global stylesheet)

Public API:
- Color constants: BG, CARD_BG, CARD_BORDER, PLOT_FG, BTN_BG, BTN_BG_DOWN, BTN_BORDER, TXT, GOOD, BAD, GRAY, PLOT_BG

Changelog:
- 2025-08-23 · 0.1.0 · KC · Initial palette sonar stack-esque.
- 2025-08-27 - 0.1.1 - JB - forked, added plot_bg to debug

To do:
- Add sonar blip/ping sound effects?
- (Greenish*) Warhammer 40k theme?

Notes:

"""

# instrument_app/theme/style.py
from .manager import theme_mgr

class _StyleProxy:
    def __getattr__(self, name: str):
        # forward any attribute lookup (e.g., TXT, BG, GOOD) to the current theme
        return getattr(theme_mgr.current, name)

style = _StyleProxy()



# ---------------- Theme (submarine dark) ----------------
BG           = "#0b2a38"
PLOT_FG      = "#7fdbff"

