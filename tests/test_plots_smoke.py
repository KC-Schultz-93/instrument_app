"""
test_plots_smoke.py
--------------------
Smoke tests for TimePressureView / LogPressureAxisItem: log-axis tick
formatting, wall-clock backfill from a fake history source, manual-pan
persistence, and smoothing. Runs headless (offscreen Qt platform). No
hardware required.

Run with:
    python tests/test_plots_smoke.py
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from PyQt5.QtWidgets import QApplication
from instrument_app.ui.plots import TimePressureView, LogPressureAxisItem
from instrument_app.services.parsing import Reading

_app = QApplication.instance() or QApplication(sys.argv)


class _FakeHistory:
    """Returns `n` synthetic points evenly spread across the requested range."""

    def __init__(self, n=5, uhv_val=1e-4, fore_val=1e-2):
        self.n = n
        self.uhv_val = uhv_val
        self.fore_val = fore_val
        self.calls = []

    def read_range(self, channel, start_ts, end_ts):
        self.calls.append((channel, start_ts, end_ts))
        if self.n == 0:
            return [], []
        step = (end_ts - start_ts) / max(self.n - 1, 1)
        times = [start_ts + i * step for i in range(self.n)]
        val = self.uhv_val if channel == "uhv" else self.fore_val
        return times, [val] * self.n


def _reading(t_s, uhv, fore):
    return Reading(t_s=t_s, uhv_torr=uhv, fore_torr=fore, tg220="Normal", tg60="Normal", maint=False)


def test_log_axis_tick_strings_and_values():
    ax = LogPressureAxisItem(orientation="left")
    strings = ax.tickStrings([-6, -5, -4, -3], 1, 1)
    assert strings == ["1e-6", "1e-5", "1e-4", "1e-3"], strings
    spacing_groups = ax.tickValues(-6, -3, 400)
    major = spacing_groups[0][1]
    assert major == [-6.0, -5.0, -4.0, -3.0], major
    print("  LogPressureAxisItem tick strings/values:  PASS")


def test_append_uses_wall_clock_not_reading_t_s():
    v = TimePressureView()
    before = time.time()
    v.append(_reading(t_s=999999.0, uhv=1e-4, fore=1e-2))
    after = time.time()
    assert before <= v._ts[-1] <= after, v._ts[-1]
    print("  append() stamps wall-clock time, ignores Reading.t_s:  PASS")


def test_backfill_unions_with_live_data():
    hist = _FakeHistory(n=5)
    v = TimePressureView(history_source=hist)
    for i in range(3):
        v.append(_reading(t_s=float(i), uhv=1e-4 * (i + 1), fore=1e-2 * (i + 1)))
    v.set_time_window(1.0)  # 1 hour - far exceeds the ~instant live span, forces backfill
    assert len(hist.calls) >= 1, "expected history_source.read_range to be called"
    assert v._current_times.size == 8, v._current_times.size  # 5 backfilled + 3 live
    print("  backfill unions historical + live data:  PASS")


def test_no_backfill_when_source_is_none():
    v = TimePressureView(history_source=None)
    for i in range(3):
        v.append(_reading(t_s=float(i), uhv=1e-4, fore=1e-2))
    v.set_time_window(1.0)
    assert v._current_times.size == 3, v._current_times.size
    print("  no history_source -> no backfill, just live data:  PASS")


def test_manual_pan_survives_new_data():
    v = TimePressureView()
    for i in range(3):
        v.append(_reading(t_s=float(i), uhv=1e-4, fore=1e-2))
    v.vb.setXRange(5, 20, padding=0.0)
    assert v._manual is True
    v.append(_reading(t_s=100.0, uhv=9e-4, fore=9e-2))
    xr = v.vb.viewRange()[0]
    assert abs(xr[0] - 5) < 1e-6 and abs(xr[1] - 20) < 1e-6, xr
    print("  manual pan/zoom survives new data:  PASS")


def test_smoothed_and_smoothed_only():
    v = TimePressureView()
    for i in range(20):
        v.append(_reading(t_s=float(i), uhv=1e-4 * (1 + 0.1 * (i % 2)), fore=1e-2))
    v.set_smoothed(True)
    assert v.smoothed_curve.xData is not None and len(v.smoothed_curve.xData) > 0
    assert v.uhv_curve.xData is not None and len(v.uhv_curve.xData) > 0
    v.set_smoothed_only(True)
    assert v.uhv_curve.xData is None or len(v.uhv_curve.xData) == 0
    print("  smoothed + smoothed-only toggle:  PASS")


def test_none_pressure_excluded_as_nan():
    v = TimePressureView()
    v.append(_reading(t_s=0.0, uhv=None, fore=1e-2))
    v.append(_reading(t_s=1.0, uhv=1e-4, fore=1e-2))
    v.set_time_window(1.0)
    assert v._current_times.size == 1, v._current_times.size
    print("  Reading with uhv_torr=None is excluded, not plotted as data:  PASS")


if __name__ == "__main__":
    print("TimePressureView / LogPressureAxisItem smoke tests (headless, no hardware)")
    print("=" * 60)
    test_log_axis_tick_strings_and_values()
    test_append_uses_wall_clock_not_reading_t_s()
    test_backfill_unions_with_live_data()
    test_no_backfill_when_source_is_none()
    test_manual_pan_survives_new_data()
    test_smoothed_and_smoothed_only()
    test_none_pressure_excluded_as_nan()
    print()
    print("All TimePressureView smoke tests passed.")
