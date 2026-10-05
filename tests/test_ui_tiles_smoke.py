"""
test_ui_tiles_smoke.py
-----------------------
Smoke tests for ToggleSwitch, ParamTile and DockHost: construction, every
theme applies, and click/open/close signals. Headless, no hardware.

Run with:
    python -m pytest tests/test_ui_tiles_smoke.py
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel

from instrument_app.theme.themes import THEMES
from instrument_app.ui import DockHost, ParamTile, ToggleSwitch

_app = QApplication.instance() or QApplication(sys.argv)


def test_all_themes_apply():
    sw, tile, dock = ToggleSwitch(), ParamTile("Scope", stepper=True), DockHost()
    dock.add_panel("a", "A", QLabel("x"))
    for theme in THEMES.values():
        for w in (sw, tile, dock):
            w.apply_theme(theme)
        sw.grab()
        tile.grab()


def test_switch_clicked_not_fired_by_set_checked():
    sw = ToggleSwitch()
    sw.show()
    fired = []
    sw.clicked.connect(lambda c: fired.append(c))
    sw.setChecked(True)
    assert fired == []
    QTest.mouseClick(sw, Qt.LeftButton)
    assert fired == [False] and not sw.isChecked()


def test_tile_body_click_vs_steppers():
    tile = ParamTile("Trigger", stepper=True)
    tile.set_values("6 mV", ["a", "b"], warn=True)
    tile.show()
    events = []
    tile.clicked.connect(lambda: events.append("body"))
    tile.step_up.connect(lambda: events.append("up"))
    tile.step_down.connect(lambda: events.append("down"))

    QTest.mouseClick(tile.btn_plus, Qt.LeftButton)
    QTest.mouseClick(tile.btn_minus, Qt.LeftButton)
    assert events == ["up", "down"]

    QTest.mouseClick(tile, Qt.LeftButton, pos=QPoint(tile.width() // 2, 3))
    assert events == ["up", "down", "body"]
    assert "6 mV" in tile.primary_text() and "⚠" in tile.primary_text()


def test_dock_one_panel_toggle():
    dock = DockHost()
    dock.add_panel("a", "A", QLabel("a"))
    dock.add_panel("b", "B", QLabel("b"))
    seen = []
    dock.changed.connect(seen.append)

    assert dock.isHidden() and dock.current_key is None
    dock.toggle("a")
    assert dock.current_key == "a" and not dock.isHidden()
    dock.toggle("b")                      # swap
    assert dock.current_key == "b"
    dock.toggle("b")                      # same tile closes
    assert dock.current_key is None and dock.isHidden()
    assert seen == ["a", "b", None]
