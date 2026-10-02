
"""
Module: instrument_app.ui.plots
Purpose: Reusable pyqtgraph plot widget for pressure vs. time, with:
         - wall-clock time axis, with historical backfill from disk so a
           multi-hour window is populated even right after opening the app,
         - clean scientific-notation log-scale pressure axis,
         - optional smoothed trend line,
         - hover readout (time/pressure at cursor, no crosshair lines),
         - RMB rubber-band zoom.

How it fits:
- Depends on: pyqtgraph, numpy, scipy.ndimage, instrument_app.theme.style,
              instrument_app.services.parsing.Reading
- Used by:    PressureInterlockPage

Public API:
- class TimePressureView(QWidget, history_source=None):
    set_view("UHV"/"Foreline"), set_time_window(hours: float),
    set_smoothed(bool), set_smoothed_only(bool), append(Reading), reset_view()

`history_source`, if given, must provide
`read_range(channel: str, start_ts: float, end_ts: float) -> (list[float], list[float])`
(see services/pressure_logger.py) - used to backfill data older than what's
currently held in memory. Pass None to use the widget standalone/in tests.

Changelog:
- 2025-08-23 · 0.1.0 · KC · Extracted plotting logic into standalone widget.
- 2025-09-10 · 0.1.1 · KC · Refactored to plot views throughout the app.
- 2026-09-21 · 0.1.2 · KC · New data no longer yanks the view back after a
  normal pan/zoom/scroll (only explicit window-select or Reset View resumes
  auto-follow). Removed the crosshair lines; kept the hover text readout.
- 2026-09-23 · 0.2.0 · KC · Edited despite CLAUDE.md's "do not touch" note on
  pressure-related functionality, with explicit user go-ahead: switched to a
  wall-clock time axis (was elapsed-time-since-connect) with CSV-backfill via
  history_source, a custom log-scale pressure axis with clean scientific-
  notation ticks (ported from an example DAQ program's LogPressureAxisItem,
  replacing pyqtgraph's built-in log mode), numeric set_time_window(hours),
  and an optional smoothed trend line.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from PyQt5.QtWidgets import QWidget, QVBoxLayout
from PyQt5.QtCore import Qt, QEvent
import pyqtgraph as pg
import numpy as np
from scipy.ndimage import uniform_filter1d
import math
import bisect
import time

from instrument_app.theme import style
from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme
from instrument_app.services.parsing import Reading

# How much live data to keep in memory before relying on history_source for
# anything older. At the firmware's ~3s reading cadence this comfortably
# covers several days - long enough that disk backfill is only needed right
# after (re)starting the app, not during normal use.
_MAX_BUFFERED_POINTS = 100_000

# Hard Y-axis limits per gauge, matching each sensor's real measurement range
# (Torr). The view can never be panned/zoomed past these.
_Y_AXIS_LIMITS_TORR = {
    "uhv": (1e-11, 1.0),
    "foreline": (1e-5, 8e2),
}


class LogPressureAxisItem(pg.AxisItem):
    """Log-scale pressure axis. Expects the data plotted against it to already
    be log10(pressure) (this widget pre-transforms before setData) and shows
    tick labels back in scientific-notation Torr."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):
        strings = []
        for v in values:
            try:
                pressure = 10 ** v
                exponent = int(np.floor(v))
                mantissa = pressure / (10 ** exponent)
                if abs(mantissa - round(mantissa)) < 0.01 and round(mantissa) in [1, 2, 3, 5, 10]:
                    mantissa_int = int(round(mantissa))
                    if mantissa_int == 1:
                        strings.append(f"1e{exponent:+d}")
                    elif mantissa_int == 10:
                        strings.append(f"1e{exponent+1:+d}")
                    else:
                        strings.append(f"{mantissa_int}e{exponent:+d}")
                else:
                    strings.append(f"{pressure:.1e}")
            except (OverflowError, ValueError):
                strings.append("")
        return strings

    def tickValues(self, minVal, maxVal, size):
        if not np.isfinite(minVal) or not np.isfinite(maxVal):
            return super().tickValues(minVal, maxVal, size)
        if minVal >= maxVal:
            return super().tickValues(minVal, maxVal, size)

        range_size = maxVal - minVal
        start_exp = int(np.floor(minVal))
        end_exp = int(np.ceil(maxVal))

        if end_exp - start_exp > 50 or end_exp - start_exp < 0:
            return super().tickValues(minVal, maxVal, size)

        if end_exp - start_exp > 15:
            step = max(1, (end_exp - start_exp) // 8)
            major_ticks = [float(exp) for exp in range(start_exp, end_exp + 1, step)
                           if minVal <= exp <= maxVal]
            return [(float(step), major_ticks)] if major_ticks else super().tickValues(minVal, maxVal, size)

        major_ticks = [float(exp) for exp in range(start_exp, end_exp + 1)
                       if minVal <= exp <= maxVal]

        minor_ticks = []
        for exp in range(start_exp - 1, end_exp + 1):
            for mult in [2, 3, 5]:
                tick_val = exp + np.log10(mult)
                if minVal <= tick_val <= maxVal:
                    minor_ticks.append(tick_val)

        fine_ticks = []
        if range_size < 1.0:
            for exp in range(start_exp - 1, end_exp + 1):
                for mult in [1.5, 2.5, 3.5, 4, 4.5, 6, 7, 8, 9]:
                    tick_val = exp + np.log10(mult)
                    if minVal <= tick_val <= maxVal:
                        fine_ticks.append(tick_val)

        result = []
        if major_ticks:
            result.append((1.0, major_ticks))
        if minor_ticks:
            result.append((0.5, minor_ticks))
        if fine_ticks:
            result.append((0.2, fine_ticks))

        return result if result else super().tickValues(minVal, maxVal, size)


class TimePressureView(QWidget):
    """Pyqtgraph plot of pressure vs. wall-clock time with live theming."""

    def __init__(self, parent: QWidget | None = None, history_source=None):
        super().__init__(parent)
        self._history_source = history_source

        # --- internal state ---
        self._view = "UHV"
        self._window_hours = 1.0 / 60.0  # default: 1 minute
        self._manual = False
        self._auto_updating = False
        self._smoothed = False
        self._smoothed_only = False
        self._ts = deque(maxlen=_MAX_BUFFERED_POINTS)
        self._uhv = deque(maxlen=_MAX_BUFFERED_POINTS)
        self._fl = deque(maxlen=_MAX_BUFFERED_POINTS)
        self._current_times = np.array([])
        self._current_values = np.array([])
        self._drag = False
        self._start = None
        self._rubber = None

        # --- build plot widget ---
        self.time_axis = pg.DateAxisItem(orientation="bottom")
        self.pressure_axis = LogPressureAxisItem(orientation="left")
        self.plot = pg.PlotWidget(axisItems={"bottom": self.time_axis, "left": self.pressure_axis})
        self.plot.setBackground(style.PLOT_BG)
        self.plot.getPlotItem().showGrid(x=True, y=True, alpha=0.5)

        # curves for UHV / Foreline pressures (log10-transformed before setData)
        self.uhv_curve = self.plot.plot(pen=pg.mkPen(style.GOOD, width=1))
        self.fl_curve = self.plot.plot(pen=pg.mkPen(style.BAD, width=1))
        self.smoothed_curve = self.plot.plot(pen=pg.mkPen(style.GOOD, width=3))

        # hover readout (time/pressure at cursor - no crosshair lines)
        self.hover = pg.TextItem(color=style.TXT)
        self.hover.hide()
        self.plot.addItem(self.hover, ignoreBounds=True)

        # viewbox + signals
        self.vb = self.plot.getPlotItem().getViewBox()
        self.vb.sigXRangeChanged.connect(self._on_user_range_change)
        self.vb.sigYRangeChanged.connect(self._on_user_range_change)
        self.plot.scene().sigMouseMoved.connect(self._on_mouse)
        self.plot.scene().installEventFilter(self)

        # lay out the widget
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.plot)

        # subscribe to theme changes
        theme_mgr.themeChanged.connect(self._apply_theme)
        self._apply_theme(theme_mgr.current)

        self._apply_y_limits()

    # --- theme hook ---
    def _apply_theme(self, t: Theme) -> None:  # pragma: no cover - pg painting
        self.plot.setBackground(t.PLOT_BG)
        self.pressure_axis.setLabel("Pressure (Torr)", color=style.TXT, **{"font-size": "12pt"})
        self.plot.setLabel("bottom", "Time", color=style.TXT, **{"font-size": "12pt"})

        pen = pg.mkPen(style.TXT)
        self.pressure_axis.setPen(pen)
        self.pressure_axis.setTextPen(pen)
        self.time_axis.setPen(pen)
        self.time_axis.setTextPen(pen)
        if hasattr(self, "hover"):
            self.hover.setColor(style.TXT)

    def set_view(self, which: str) -> None:
        self._view = which
        self._apply_y_limits()
        self._update()

    def _apply_y_limits(self) -> None:
        channel = "foreline" if self._view == "Foreline" else "uhv"
        lo, hi = _Y_AXIS_LIMITS_TORR[channel]
        self.vb.setLimits(yMin=math.log10(lo), yMax=math.log10(hi))

    def set_time_window(self, hours: float) -> None:
        self._window_hours = float(hours)
        self._manual = False
        self._update()

    def set_smoothed(self, enabled: bool) -> None:
        self._smoothed = bool(enabled)
        self._update()

    def set_smoothed_only(self, enabled: bool) -> None:
        self._smoothed_only = bool(enabled)
        self._update()

    def reset_view(self) -> None:
        """Drop any manual (rubber-band) zoom and resume auto-following the
        selected time window - undoes what a right-drag zoom leaves stuck."""
        self._manual = False
        self._update()

    def append(self, r: Reading) -> None:
        self._ts.append(time.time())
        self._uhv.append(r.uhv_torr if r.uhv_torr is not None else math.nan)
        self._fl.append(r.fore_torr if r.fore_torr is not None else math.nan)
        self._update()

    # ---- internals ----
    def _get_window_data(self, channel: str) -> tuple[np.ndarray, np.ndarray]:
        """Times/values for `channel` ('uhv'/'foreline') covering the current
        time window, backfilling from history_source if the in-memory buffer
        doesn't go back far enough."""
        window_seconds = self._window_hours * 3600.0
        now = time.time()
        cutoff = now - window_seconds

        mem_vals = self._uhv if channel == "uhv" else self._fl
        times = list(self._ts)
        values = list(mem_vals)
        mem_oldest = times[0] if times else now

        if self._history_source is not None and cutoff < mem_oldest:
            hist_times, hist_values = self._history_source.read_range(channel, cutoff, mem_oldest - 1)
            if hist_times:
                times = list(hist_times) + times
                values = list(hist_values) + values

        times_arr = np.array(times, dtype=float)
        values_arr = np.array(values, dtype=float)
        if times_arr.size:
            mask = times_arr >= cutoff
            times_arr = times_arr[mask]
            values_arr = values_arr[mask]
        return times_arr, values_arr

    def _update(self) -> None:
        if not self._ts:
            return

        channel = "foreline" if self._view == "Foreline" else "uhv"
        if self._manual:
            times = np.array(self._ts, dtype=float)
            values = np.array(self._fl if channel == "foreline" else self._uhv, dtype=float)
        else:
            times, values = self._get_window_data(channel)

        active, other = (self.fl_curve, self.uhv_curve) if channel == "foreline" else (self.uhv_curve, self.fl_curve)
        other.setData([], [])

        finite = np.isfinite(values) & (values > 0)
        t_f = times[finite]
        v_f = values[finite]
        self._current_times = t_f
        self._current_values = v_f

        if t_f.size == 0:
            active.setData([], [])
            self.smoothed_curve.setData([], [])
            return

        y_log = np.log10(v_f)
        active.setData([], []) if self._smoothed_only else active.setData(t_f, y_log)

        line_color = style.BAD if channel == "foreline" else style.GOOD
        if self._smoothed and t_f.size > 10:
            window_size = min(max(61, t_f.size // 50), 301)
            if window_size % 2 == 0:
                window_size += 1
            y_smooth = uniform_filter1d(y_log, size=window_size, mode="nearest")
            self.smoothed_curve.setPen(pg.mkPen(color=line_color, width=3))
            self.smoothed_curve.setData(t_f, y_smooth)
            # Dim the raw curve when the smoothed overlay is also shown.
            if not self._smoothed_only:
                raw_color = pg.mkColor(line_color)
                raw_color.setAlpha(80)
                active.setPen(pg.mkPen(color=raw_color, width=1))
        else:
            self.smoothed_curve.setData([], [])
            active.setPen(pg.mkPen(color=line_color, width=1))

        if not self._manual:
            self._auto_updating = True
            try:
                self.vb.setXRange(t_f[0], t_f[-1], padding=0.02)
                pad = max(0.1, 0.1 * (y_log.max() - y_log.min()))
                self.vb.setYRange(y_log.min() - pad, y_log.max() + pad, padding=0.0)
            finally:
                self._auto_updating = False

    def _on_user_range_change(self, *_):
        # A range change we didn't just issue ourselves in _update() means the
        # user panned/zoomed/scrolled - stop auto-following so new data
        # doesn't yank their view back.
        if not self._auto_updating:
            self._manual = True

    def _on_mouse(self, pos):
        if self._current_times.size == 0:
            return
        if not self.plot.sceneBoundingRect().contains(pos):
            self.hover.hide()
            return
        mp = self.vb.mapSceneToView(pos)
        x = float(mp.x())
        y = float(mp.y())
        xs = self._current_times
        i = bisect.bisect_left(xs, x)
        idx = 0 if i <= 0 else (len(xs) - 1 if i >= len(xs) else (i if abs(xs[i] - x) < abs(x - xs[i - 1]) else i - 1))
        px = xs[idx]
        py = self._current_values[idx]
        if py is None or (isinstance(py, float) and (math.isnan(py) or py <= 0)):
            self.hover.hide()
            return
        t_str = datetime.fromtimestamp(px).strftime("%Y-%m-%d %H:%M:%S")
        self.hover.setText(f"{t_str}\n{py:.2E} Torr")
        span = abs(self.vb.viewRange()[0][1] - self.vb.viewRange()[0][0])
        self.hover.setPos(mp.x() + 0.01 * span, y)
        self.hover.show()

    # RMB rubber band zoom (same behavior as before)
    def eventFilter(self, obj, ev):  # noqa: N802 - Qt override
        if obj is self.plot.scene():
            et = ev.type()
            to_view = self.vb.mapSceneToView
            if et == QEvent.GraphicsSceneMousePress and ev.button() == Qt.RightButton:
                sp = ev.scenePos()
                if self.plot.sceneBoundingRect().contains(sp):
                    self._drag = True
                    self._start = to_view(sp)
                    if not self._rubber:
                        self._rubber = pg.RectROI([self._start.x(), self._start.y()], [1e-6, 1e-6],
                            pen=pg.mkPen('#ffffff', width=1, style=Qt.DashLine),
                            brush=pg.mkBrush(127,219,255,60))
                        self._rubber.setZValue(10)
                        self._rubber.setMovable(False)
                        self._rubber.setRotatable(False)
                        self._rubber.setResizable(False)
                        self.plot.addItem(self._rubber)
                    else:
                        self._rubber.show()
                        self._rubber.setPos([self._start.x(), self._start.y()])
                        self._rubber.setSize([1e-6, 1e-6])
                    ev.accept()
                    return True
            if et == QEvent.GraphicsSceneMouseMove and self._drag:
                cur = to_view(ev.scenePos())
                x0, x1 = sorted([self._start.x(), cur.x()])
                y0, y1 = sorted([self._start.y(), cur.y()])
                self._rubber.setPos([x0, y0])
                self._rubber.setSize([max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)])
                ev.accept()
                return True
            if et == QEvent.GraphicsSceneMouseRelease and self._drag and ev.button() == Qt.RightButton:
                cur = to_view(ev.scenePos())
                x0, x1 = sorted([self._start.x(), cur.x()])
                y0, y1 = sorted([self._start.y(), cur.y()])
                if (x1 - x0) > 1e-6 and (y1 - y0) > 1e-9:
                    self.vb.setXRange(x0, x1, padding=0.0)
                    self.vb.setYRange(y0, y1, padding=0.0)
                    self._manual = True
                if self._rubber:
                    self._rubber.hide()
                    self._drag = False
                    self._start = None
                    ev.accept()
                return True
        return super().eventFilter(obj, ev)
