# instrument_app/pages/cdms_page.py
"""
Module: instrument_app.pages.cdms_page
Purpose: Live CDMS acquisition UI (controls + event ingest + real-time analysis).
         Works out-of-the-box with a synthetic generator. Later, plug in a real
         digitizer/FPGA feed and an NI-DAQ service for electrode control.

How it fits:
- Depends on: numpy, pyqtgraph, PyQt5, instrument_app.theme.style
- Used by:    MainWindow (as the "CDMS" tab)

Public API:
- class CDMSPage(QWidget): optional param daq with methods:
    - set_voltage(chan: str, volts: float, ramp_ms: int)
    - write_do(line: str, level: bool)
"""
from __future__ import annotations

import math
import time
import random
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from numpy.fft import rfft

from PyQt5.QtCore import (
    Qt, QObject, QThread, pyqtSignal, pyqtSlot, QTimer, QDateTime
)
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QFormLayout,
    QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QCheckBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QSplitter
)
import pyqtgraph as pg

from instrument_app.theme import style

# --- stronger foreground color (fallback to TXT if TXT_STRONG isn't defined) ---
TXT_FG = getattr(style, "TXT_STRONG", style.TXT)


# ----------------------------- Workers --------------------------------- #

class SyntheticGenerator(QObject):
    """
    Emits synthetic int16 blocks that mimic CDMS events.
    block_ready(data:int16 ndarray, fs_hz:float)
    """
    block_ready = pyqtSignal(object, float)
    status = pyqtSignal(str)

    def __init__(self, *, fs_hz: float = 2_400_000.0, n_samples: int = 262_144,
                 empty_prob: float = 0.50, multiple_prob: float = 0.15,
                 f0_range: Tuple[float, float] = (20_000.0, 120_000.0),
                 snr_db: float = 20.0, period_ms: int = 250):
        super().__init__()
        self.fs = float(fs_hz)
        self.N = int(n_samples)
        self.empty_prob = float(empty_prob)
        self.multiple_prob = float(multiple_prob)
        self.f0_range = f0_range
        self.snr_db = float(snr_db)
        self.period_ms = int(period_ms)
        self._running = False

    @pyqtSlot()
    def start(self):
        self._running = True
        # prebuild time base (float32 for FFT)
        t = np.arange(self.N, dtype=np.float32) / self.fs
        while self._running:
            u = random.random()
            # noise baseline
            noise_rms = 0.05
            noise = np.random.normal(0.0, noise_rms, size=self.N).astype(np.float32)

            if u < self.empty_prob:
                x = noise
            else:
                # single or multiple ion(s)
                f0 = random.uniform(*self.f0_range)
                amp = noise_rms * (10 ** (self.snr_db / 20.0))
                x = amp * np.sin(2 * np.pi * f0 * t, dtype=np.float32)
                # add harmonics
                x += 0.35 * amp * np.sin(2 * np.pi * 2 * f0 * t, dtype=np.float32)
                x += 0.20 * amp * np.sin(2 * np.pi * 3 * f0 * t, dtype=np.float32)

                if u > (1.0 - self.multiple_prob):
                    # add a nearby second ion (multiple)
                    f1 = f0 * random.uniform(1.08, 1.20)
                    x += 0.8 * amp * np.sin(2 * np.pi * f1 * t, dtype=np.float32)

                x += noise

            # scale to int16 with saturation
            x16 = np.clip(x * 1000.0, -32767, 32767).astype(np.int16)
            self.block_ready.emit(x16, self.fs)
            QThread.msleep(self.period_ms)

    @pyqtSlot()
    def stop(self):
        self._running = False


@dataclass
class EventResult:
    cls: str                     # "no_ion" | "single" | "multiple"
    f0_hz: Optional[float]       # None for no_ion
    snr_db: Optional[float]
    n_peaks: int
    timestamp: float             # epoch seconds


class Analyzer(QObject):
    """
    FFT-based classifier. Implements "6× RMS noise" threshold and a light
    harmonic sanity check.
    """
    event_result = pyqtSignal(object)

    @pyqtSlot(object, float)
    def analyze_block(self, x_i16: np.ndarray, fs_hz: float):
        ts = time.time()
        N = int(1 << int(np.ceil(np.log2(len(x_i16)))))
        x = np.empty(N, np.float32)
        n0 = len(x_i16)
        x[:n0] = x_i16.astype(np.float32)
        x[n0:] = 0.0

        # high-pass (DC removal)
        x -= np.mean(x)

        mag = np.abs(rfft(x))
        # crude noise estimate from upper spectrum (ignore first ~60%)
        start = int(0.6 * len(mag))
        noise_rms = float(np.std(mag[start:])) if start < len(mag) else float(np.std(mag))
        thr = 6.0 * noise_rms

        peaks = np.where(mag > thr)[0]
        n_peaks = int(len(peaks))
        if n_peaks == 0:
            self.event_result.emit(EventResult("no_ion", None, None, 0, ts))
            return

        # pick the strongest bin as candidate f0
        k0 = int(peaks[np.argmax(mag[peaks])])
        f0 = k0 * fs_hz / N

        # SNR ~ peak / noise_rms → dB
        snr = float(mag[k0] / (noise_rms + 1e-12))
        snr_db = 20.0 * math.log10(max(snr, 1e-9))

        # simple harmonic check: look for energy near 2*f0 and 3*f0
        def near_bin(f):
            return int(round(f * N / fs_hz))

        hits = 0
        for mult, frac_bw in [(2, 0.015), (3, 0.02)]:
            km = near_bin(mult * f0)
            lo = max(0, int(km * (1 - frac_bw)))
            hi = min(len(mag) - 1, int(km * (1 + frac_bw)))
            if np.max(mag[lo:hi+1]) > thr:
                hits += 1

        cls = "single" if hits >= 1 else ("multiple" if n_peaks >= 2 else "single")
        self.event_result.emit(EventResult(cls, f0, snr_db, n_peaks, ts))


# ----------------------------- UI Page --------------------------------- #

class CDMSPage(QWidget):
    """
    UI page for CDMS:
    - Left: electrode controls (AO) + digital lines (DO) + acquisition controls.
    - Right: event table (top) + histogram (bottom).
    - Works without hardware; pass a 'daq' service to enable AO/DO.
    """
    def __init__(self, daq: Optional[object] = None):
        super().__init__()
        self.daq = daq

        # Ensure dark background + high-contrast foreground cascade to children
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QWidget {{ background:{style.BG}; color:{TXT_FG}; }}
            QGroupBox {{ color:{TXT_FG}; border:1px solid {style.CARD_BORDER}; border-radius:8px; padding:6px; }}
            QLabel {{ color:{TXT_FG}; }}
        """)

        self._events_seen = 0
        self._counts = {"no_ion": 0, "single": 0, "multiple": 0}
        self._f0_hist_vals = []  # store Hz for singles

        root = QHBoxLayout(self); root.setContentsMargins(10, 8, 10, 10); root.setSpacing(10)

        # Left controls
        left = QVBoxLayout(); left.setSpacing(10)
        left.addWidget(self._build_ao_group())
        left.addWidget(self._build_do_group())
        left.addWidget(self._build_acq_group())
        left.addStretch(1)

        # Right content: table (top) + histogram (bottom)
        right = QVBoxLayout(); right.setSpacing(8)
        self.table = self._build_table()
        self.hist_plot = self._build_hist_plot()
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.hist_plot)
        split.setSizes([400, 300])
        right.addWidget(split)

        # Counters row
        ctr = QHBoxLayout()
        self.lbl_empty = QLabel("Empty: 0"); self._pillify(self.lbl_empty, "#7f8c8d", TXT_FG)
        self.lbl_single = QLabel("Single: 0"); self._pillify(self.lbl_single, style.GOOD, "#0b2a38")  # dark text on bright green
        self.lbl_multi  = QLabel("Multiple: 0"); self._pillify(self.lbl_multi, "#ff4136", "#0b2a38")  # dark text on bright red
        self.lbl_rate   = QLabel("Rate: 0.0 evt/s"); self._pillify(self.lbl_rate, style.CARD_BG, TXT_FG)
        ctr.addWidget(self.lbl_empty); ctr.addWidget(self.lbl_single); ctr.addWidget(self.lbl_multi); ctr.addStretch(1); ctr.addWidget(self.lbl_rate)
        right.addLayout(ctr)

        root.addLayout(left, 0)
        root.addLayout(right, 1)

        # --- Workers & threads ---
        self.gen_thread = QThread(); self.gen = SyntheticGenerator()
        self.rt_thread  = QThread(); self.rt  = Analyzer()
        self.gen.moveToThread(self.gen_thread)
        self.rt.moveToThread(self.rt_thread)

        # Connect pipeline: generator → analyzer → UI
        self.gen.block_ready.connect(self.rt.analyze_block, Qt.QueuedConnection)
        self.rt.event_result.connect(self._on_event_result, Qt.QueuedConnection)

        # Start analyzer thread immediately; start generator on "Start"
        self.rt_thread.start()

        # UI timers
        self._t0 = time.time()
        self._last_n = 0
        self.rate_timer = QTimer(self); self.rate_timer.setInterval(1000)
        self.rate_timer.timeout.connect(self._update_rate)
        self.rate_timer.start()

        # Disable AO/DO if no DAQ provided
        if self.daq is None:
            self.gb_ao.setTitle(self.gb_ao.title() + " (no DAQ)")
            self.gb_do.setTitle(self.gb_do.title() + " (no DAQ)")
            for w in (self.sp_ao0, self.sp_ao1, self.sp_ramp, self.btn_apply_ao,
                      self.chk_do0, self.chk_do1, self.btn_pulse):
                w.setEnabled(False)

    # ---------------------- UI Builders ---------------------- #

    def _pillify(self, label: QLabel, bg: str, fg: str):
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(
            f"QLabel{{background:{bg}; color:{fg}; padding:4px 8px; border-radius:8px; font:10pt 'Segoe UI';}}"
        )

    def _build_ao_group(self) -> QGroupBox:
        gb = QGroupBox("Electrode Voltages (AO)")
        form = QFormLayout(gb); form.setLabelAlignment(Qt.AlignRight); form.setContentsMargins(8,8,8,8); form.setSpacing(6)

        self.sp_ao0 = QDoubleSpinBox(); self._sty_spin(self.sp_ao0, -10.0, 10.0, 0.01, 0.00); self.sp_ao0.setSuffix(" V")
        self.sp_ao1 = QDoubleSpinBox(); self._sty_spin(self.sp_ao1, -10.0, 10.0, 0.01, 0.00); self.sp_ao1.setSuffix(" V")
        self.sp_ramp = QSpinBox(); self._sty_spin(self.sp_ramp, 1, 2000, 1, 100); self.sp_ramp.setSuffix(" ms")

        self.btn_apply_ao = QPushButton("Apply"); self._sty_btn(self.btn_apply_ao)
        self.btn_apply_ao.clicked.connect(self._apply_ao_clicked)

        form.addRow("AO0 (Endcap A):", self.sp_ao0)
        form.addRow("AO1 (Endcap B):", self.sp_ao1)
        form.addRow("Ramp time:", self.sp_ramp)
        form.addRow("", self.btn_apply_ao)

        self.gb_ao = gb
        return gb

    def _build_do_group(self) -> QGroupBox:
        gb = QGroupBox("Digital Lines (DO)")
        lay = QGridLayout(gb); lay.setContentsMargins(8,8,8,8); lay.setSpacing(6)

        self.chk_do0 = QCheckBox("port0/line0"); self._sty_chk(self.chk_do0)
        self.chk_do1 = QCheckBox("port0/line1"); self._sty_chk(self.chk_do1)
        self.chk_do0.stateChanged.connect(lambda s: self._write_do("port0/line0", s == Qt.Checked))
        self.chk_do1.stateChanged.connect(lambda s: self._write_do("port0/line1", s == Qt.Checked))

        self.btn_pulse = QPushButton("Pulse line0 (50 ms)"); self._sty_btn(self.btn_pulse)
        self.btn_pulse.clicked.connect(lambda: self._pulse_do("port0/line0", 50))

        lay.addWidget(self.chk_do0, 0, 0)
        lay.addWidget(self.chk_do1, 0, 1)
        lay.addWidget(self.btn_pulse, 1, 0, 1, 2)
        self.gb_do = gb
        return gb

    def _build_acq_group(self) -> QGroupBox:
        gb = QGroupBox("Acquisition")
        lay = QGridLayout(gb); lay.setContentsMargins(8,8,8,8); lay.setSpacing(6)

        self.chk_synth = QCheckBox("Use synthetic generator"); self.chk_synth.setChecked(True); self._sty_chk(self.chk_synth)

        self.sp_fs = QDoubleSpinBox(); self._sty_spin(self.sp_fs, 100_000, 5_000_000, 1_000, 2_400_000); self.sp_fs.setSuffix(" Hz")
        self.sp_N  = QSpinBox(); self._sty_spin(self.sp_N, 16_384, 1_048_576, 1024, 262_144)
        self.sp_period = QSpinBox(); self._sty_spin(self.sp_period, 10, 2000, 10, 250); self.sp_period.setSuffix(" ms")

        self.btn_start = QPushButton("Start"); self._sty_btn(self.btn_start)
        self.btn_stop  = QPushButton("Stop");  self._sty_btn(self.btn_stop); self.btn_stop.setEnabled(False)

        self.btn_start.clicked.connect(self._start_clicked)
        self.btn_stop.clicked.connect(self._stop_clicked)

        lay.addWidget(self.chk_synth, 0, 0, 1, 2)
        lay.addWidget(QLabel("Sample rate:"), 1, 0); lay.addWidget(self.sp_fs, 1, 1)
        lay.addWidget(QLabel("Samples per event:"), 2, 0); lay.addWidget(self.sp_N, 2, 1)
        lay.addWidget(QLabel("Event period:"), 3, 0); lay.addWidget(self.sp_period, 3, 1)
        lay.addWidget(self.btn_start, 4, 0); lay.addWidget(self.btn_stop, 4, 1)
        return gb

    def _build_table(self) -> QTableWidget:
        tbl = QTableWidget(0, 6)
        tbl.setHorizontalHeaderLabels(["Time", "Class", "f0 (kHz)", "SNR (dB)", "#Peaks", "Notes"])
        hh = tbl.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        # Higher-contrast table + header
        tbl.setStyleSheet(
            f"QTableWidget{{background:{style.CARD_BG}; color:{TXT_FG}; gridline-color:{style.CARD_BORDER};}}"
            f"QHeaderView::section{{background:{style.BTN_BG}; color:{TXT_FG}; "
            f"border:1px solid {style.BTN_BORDER}; padding:4px; font-weight:600;}}"
        )
        return tbl

    def _build_hist_plot(self) -> QWidget:
        pg.setConfigOptions(background=style.BG, foreground=TXT_FG)
        w = pg.PlotWidget()
        w.setLabel('left', 'Count')
        w.setLabel('bottom', 'f0 (kHz)')
        w.showGrid(x=True, y=True, alpha=0.25)
        self._hist_curve = pg.BarGraphItem(x=[], height=[], width=1.0)
        w.addItem(self._hist_curve)
        return w

    # ---------------------- Styling helpers ---------------------- #

    def _sty_btn(self, b: QPushButton):
        b.setStyleSheet(
            f"QPushButton{{color:{TXT_FG}; background:{style.BTN_BG}; border:1px solid {style.BTN_BORDER}; "
            f"padding:6px 10px; border-radius:8px; font:10pt 'Segoe UI';}}"
            f"QPushButton:pressed{{background:{style.BTN_BG_DOWN};}}"
        )

    def _sty_spin(self, sp, mn, mx, step, val):
        if isinstance(sp, QDoubleSpinBox):
            sp.setRange(float(mn), float(mx)); sp.setSingleStep(float(step)); sp.setDecimals(3); sp.setValue(float(val))
        else:
            sp.setRange(int(mn), int(mx)); sp.setSingleStep(int(step)); sp.setValue(int(val))
        sp.setButtonSymbols(QDoubleSpinBox.NoButtons)
        sp.setStyleSheet(
            f"QDoubleSpinBox,QSpinBox{{color:{TXT_FG}; background:{style.BTN_BG}; border:1px solid {style.BTN_BORDER}; "
            f"padding:4px 8px; border-radius:8px; font:10pt 'Segoe UI'; min-width:120px;}}"
        )

    def _sty_chk(self, chk: QCheckBox):
        chk.setStyleSheet(f"QCheckBox{{color:{TXT_FG}; font:10pt 'Segoe UI';}}")

    # ---------------------- Slots / Handlers ---------------------- #

    def _apply_ao_clicked(self):
        if self.daq is None:
            return
        ramp = int(self.sp_ramp.value())
        self.daq.set_voltage("ao0", float(self.sp_ao0.value()), ramp)
        self.daq.set_voltage("ao1", float(self.sp_ao1.value()), ramp)

    def _write_do(self, line: str, level: bool):
        if self.daq is None:
            return
        self.daq.write_do(line, level)

    def _pulse_do(self, line: str, width_ms: int):
        if self.daq is None:
            return
        self.daq.write_do(line, True)
        QTimer.singleShot(width_ms, lambda: self.daq.write_do(line, False))

    def _start_clicked(self):
        # apply generator params
        self.gen.fs = float(self.sp_fs.value())
        self.gen.N = int(self.sp_N.value())
        self.gen.period_ms = int(self.sp_period.value())

        # reset counters/plots
        self._events_seen = 0
        self._counts = {"no_ion": 0, "single": 0, "multiple": 0}
        self._f0_hist_vals.clear()
        self._update_counters()

        if self.chk_synth.isChecked():
            if not self.gen_thread.isRunning():
                self.gen_thread.started.connect(self.gen.start, Qt.QueuedConnection)
                self.gen_thread.start()
        # else: hook your real ingest here

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

    def _stop_clicked(self):
        if self.gen_thread.isRunning():
            self.gen.stop()
            QThread.msleep(20)
            self.gen_thread.quit()
            self.gen_thread.wait()
            try:
                self.gen_thread.started.disconnect(self.gen.start)
            except Exception:
                pass
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    @pyqtSlot(object)
    def _on_event_result(self, res: EventResult):
        self._events_seen += 1
        self._counts[res.cls] = self._counts.get(res.cls, 0) + 1

        # table append (cap at ~500 rows)
        dt = QDateTime.fromMSecsSinceEpoch(int(res.timestamp * 1000)).toString("hh:mm:ss.zzz")
        row = self.table.rowCount()
        self.table.insertRow(row)
        def setc(col, text):
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, col, it)
        setc(0, dt)
        setc(1, res.cls)
        setc(2, f"{(res.f0_hz or 0.0)/1000.0:,.1f}" if res.f0_hz else "-")
        setc(3, f"{res.snr_db:.1f}" if res.snr_db is not None else "-")
        setc(4, str(res.n_peaks))
        setc(5, "")

        if self.table.rowCount() > 500:
            self.table.removeRow(0)

        # histogram only for "single"
        if res.cls == "single" and res.f0_hz:
            self._f0_hist_vals.append(res.f0_hz / 1000.0)  # kHz
            if len(self._f0_hist_vals) % 5 == 0:  # throttle updates
                self._refresh_hist()

        self._update_counters()

    def _update_counters(self):
        self.lbl_empty.setText(f"Empty: {self._counts['no_ion']}")
        self.lbl_single.setText(f"Single: {self._counts['single']}")
        self.lbl_multi.setText(f"Multiple: {self._counts['multiple']}")

    def _update_rate(self):
        now = time.time()
        dt = max(now - self._t0, 1e-3)
        n = self._events_seen
        rate = (n - self._last_n) / dt
        self.lbl_rate.setText(f"Rate: {rate:,.1f} evt/s")
        self._t0 = now
        self._last_n = n

    def _refresh_hist(self):
        if not self._f0_hist_vals:
            self._hist_curve.setOpts(x=[], height=[], width=1.0)
            return
        vals = np.array(self._f0_hist_vals, dtype=np.float32)
        vmin, vmax = float(np.min(vals)), float(np.max(vals))
        if vmin == vmax:
            vmin -= 0.5; vmax += 0.5
        bins = max(20, min(80, int(max(10.0, (vmax - vmin)))))  # ~20-80 bins
        hist, edges = np.histogram(vals, bins=bins, range=(vmin, vmax))
        x = (edges[:-1] + edges[1:]) * 0.5
        self._hist_curve.setOpts(x=x, height=hist, width=(edges[1] - edges[0]) * 0.9)

    # ---------------------- Qt lifecycle ---------------------- #

    def closeEvent(self, ev):
        # ensure threads stop if user closes the tab/window
        self._stop_clicked()
        super().closeEvent(ev)

