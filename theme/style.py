from .manager import theme_mgr

class _StyleProxy:
    def __getattr__(self, name: str):
        cur = theme_mgr.current
        # Legacy alias used by older widgets
        if name == "PLOT_FG":
            return cur.TXT
        return getattr(cur, name)

style = _StyleProxy()
