# instrument_app/pages/cdms_page.py
"""
CDMS tab: controls (AO/DO), acquisition source (Synthetic or PicoScope), and real-time analysis.
Synthetic works out of the box; PicoScope sources are enabled once you add a scope service.

Public API:
- class CDMSPage(QWidget, daq: Optional object with set_voltage/write_do)

Changelog:
- 2025-08-25 · 0.2.0 · Add Source selector (Synthetic/PicoScope), keep synthetic default.
"""
from __future__ import annotations

import math, time, random
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from numpy.fft import rfft

from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal, pyqtSlot, QTimer, QDateTime
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QFormLayout,
    QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QCheckBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QSplitter, QComboBox, QMessageBox
)
import pyqtgraph as pg

from instrument_app.theme import style

# Stronger foreground (fallback to TXT)
TXT_FG = getattr(style, "TXT_STRONG", style.TXT)

# --- Optional PicoScope support ---
HAVE_PICO = False
try:
    # If/when you add a real service as instrument_app.services.scope_pico.PicoScopeService
    from instrument_app.services.scope_pico import PicoScopeService  # type: ignore
    HAVE_PICO = True
except Exception:
    # Safe no-op stub so imports never break
    class PicoScopeService(QObject):  # type: ignore
        block_ready = pyqtSignal(object, float)
        status = pyqtSignal(str)
        @pyqtSlot()
        def start_rapid_block(self): self.status.emit("PicoSDK/service not installed.")
        @pyqtSlot()
        def start_streaming(self):   self.status.emit("PicoSDK/service not installed.")
        @pyqtSlot()
        def stop(self): pass


# ----------------------------- Workers --------------------------------- #

class SyntheticGenerator(QObject):
    """Emits synthetic int16 blocks that mimic CDMS events."""
    block_ready = pyqtSignal(object, float)
    status = pyqtSignal(str)

    def __init__(self, *, fs_hz: float = 2_400_000.0, n_samples: int = 262_144,
                 empty_prob: float = 0.50, multiple_prob: float = 0.15,
                 f0_range: Tuple[float, float] = (20_000.0, 120_000.0),
                 snr_db: float = 20.0, period_ms: int = 250):
        super().__init__()
        self.fs = float(fs_hz); self.N = int(n_samples)
        self.empty_prob = float(empty_prob); self.multiple_prob = float(multiple_prob)
        self.f0_range = f0_range; self.snr_db = float(snr_db)
        self.period_ms = int(period_ms); self._running = False

    @pyqtSlot()
    def start(self):
        self._running = True
        t = np.arange(self.N, dtype=np.float32) / self.fs
        self.status.emit(f"Synthetic: fs={self.fs:.0f} Hz, N={self.N}, period={self.period_ms} ms")
        while self._running:
            u = random.random()
            noise_rms = 0.05
            noise = np.random.normal(0.0, noise_rms, size=self.N).astype(np.float32)
            if u < self.empty_prob:
                x = noise
            else:
                f0 = random.uniform(*self.f0_range)
                amp = noise_rms * (10 ** (self.snr_db / 20.0))
                x = amp * np.sin(2 * np.pi * f0 * t, dtype=np.float32)
                x += 0.35 * amp * np.sin(2 * np.pi * 2 * f0 * t, dtype=np.float32)
                x += 0.20 * amp * np.sin(2 * np.pi * 3 * f0 * t, dtype=np.float32)
                if u > (1.0 - self.multiple_prob):
                    f1 = f0 * random.uniform(1.08, 1.20)
                    x += 0.8 * amp * np.sin(2 * np.pi * f1 * t, dtype=np.float32)
                x += noise
            x16 = np.clip(x * 1000.0, -32767, 32767).astype(np.int16)
            self.block_ready.emit(x16, self.fs)
            QThread.msleep(self.period_ms)

    @pyqtSlot()
    def stop(self): self._running = False


@dataclass
class EventResult:
    cls: str                     # "no_ion" | "single" | "multiple"
    f0_hz: Optional[float]
    snr_db: Optional[float]
    n_peaks: int
    timestamp: float


class Analyzer(QObject):
    """FFT + 6×RMS threshold + light harmonic check; emits EventResult."""
    event_result = pyqtSignal(object)

    @pyqtSlot(object, float)
    def analyze_block(self, x_i16: np.ndarray, fs_hz: float):
        ts = time.time()
        N = int(1 << int(np.ceil(np.log2(len(x_i16)))))
        x = np.empty(N, np.float32); n0 = len(x_i16)
        x[:n0] = x_i16.astype(np.float32); x[n0:] = 0.0
        x -= np.mean(x)

        mag = np.abs(rfft(x))
        start = int(0.6 * len(mag))
        noise_rms = float(np.std(mag[start:])) if start < len(mag) else float(np.std(mag))
        thr = 6.0 * noise_rms

        peaks = np.where(mag > thr)[0]; n_peaks = int(len(peaks))
        if n_peaks == 0:
            self.event_result.emit(EventResult("no_ion", None, None, 0, ts)); return

        k0 = int(peaks[np.argmax(mag[peaks])]); f0 = k0 * fs_hz / N
        snr = float(mag[k0] / (noise_rms + 1e-12)); snr_db = 20.0 * math.log10(max(snr, 1e-9))

        def near_bin(f): return int(round(f * N / fs_hz))
        hits = 0
        for mult, frac_bw in [(2, 0.015), (3, 0.02)]:
            km = near_bin(mult * f0)
            lo = max(0, int(km * (1 - frac_bw))); hi = min(len(mag) - 1, int(km * (1 + frac_bw)))
            if np.max(mag[lo:hi+1]) > thr: hits += 1

        cls = "single" if hits >= 1 else ("multiple" if n_peaks >= 2 else "single")
        self.event_result.emit(EventResult(cls, f0, snr_db, n_peaks, ts))


# ----------------------------- UI Page --------------------------------- #

class CDMSPage(QWidget):
    """
    Left: AO/DO controls + Acquisition (Source selector). Right: event table + histogram + counters.
    Synthetic source is always available; PicoScope sources appear but are disabled until the service exists.
    """
    def __init__(self, daq: Optional[object] = None):
        super().__init__()
        self.daq = daq

        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QWidget {{ background:{style.BG}; color:{TXT_FG}; }}
            QGroupBox {{ color:{TXT_FG}; border:1px solid {style.CARD_BORDER}; border-radius:8px; padding:6px; }}
            QLabel {{ color:{TXT_FG}; }}
        """)

        self._events_seen = 0
        self._counts = {"no_ion": 0, "single": 0, "multiple": 0}
        self._f0_hist_vals: list[float] = []

        root = QHBoxLayout(self); root.setContentsMargins(10,8,10,10); root.setSpacing(10)

        # Left controls
        left = QVBoxLayout(); left.setSpacing(10)
        left.addWidget(self._build_ao_group())
        left.addWidget(self._build_do_group())
        left.addWidget(self._build_acq_group())
        left.addStretch(1)

        # Right: table + histogram
        right = QVBoxLayout(); right.setSpacing(8)
        self.table = self._build_table()
        self.hist_plot = self._build_hist_plot()
        split = QSplitter(Qt.Vertical); split.addWidget(self.table); split.addWidget(self.hist_plot); split.setSizes([400,300])
        right.addWidget(split)

        # Counters row
        ctr = QHBoxLayout()
        self.lbl_empty = QLabel("Empty: 0"); self._pillify(self.lbl_empty, "#7f8c8d", TXT_FG)
        self.lbl_single = QLabel("Single: 0"); self._pillify(self.lbl_single, style.GOOD, "#0b2a38")
        self.lbl_multi  = QLabel("Multiple: 0"); self._pillify(self.lbl_multi, "#ff4136", "#0b2a38")
        self.lbl_rate   = QLabel("Rate: 0.0 evt/s"); self._pillify(self.lbl_rate, style.CARD_BG, TXT_FG)
        ctr.addWidget(self.lbl_empty); ctr.addWidget(self.lbl_single); ctr.addWidget(self.lbl_multi); ctr.addStretch(1); ctr.addWidget(self.lbl_rate)
        right.addLayout(ctr)

        root.addLayout(left, 0); root.addLayout(right, 1)

        # Threads & workers
        self.gen_thread = QThread(); self.gen = SyntheticGenerator()
        self.rt_thread  = QThread(); self.rt  = Analyzer()
        self.gen.moveToThread(self.gen_thread); self.rt.moveToThread(self.rt_thread)
        self.gen.block_ready.connect(self.rt.analyze_block, Qt.QueuedConnection)
        self.rt.event_result.connect(self._on_event_result, Qt.QueuedConnection)
        self.rt_thread.start()

        # Pico members (created on demand)
        self.pico_thread: Optional[QThread] = None
        self.pico: Optional[PicoScopeService] = None

        # Rate timer
        self._t0 = time.time(); self._last_n = 0
        self.rate_timer = QTimer(self); self.rate_timer.setInterval(1000)
        self.rate_timer.timeout.connect(self._update_rate); self.rate_timer.start()

        # Disable AO/DO if no DAQ
        if self.daq is None:
            self.gb_ao.setTitle(self.gb_ao.title() + " (no DAQ)")
            self.gb_do.setTitle(self.gb_do.title() + " (no DAQ)")
            for w in (self.sp_ao0, self.sp_ao1, self.sp_ramp, self.btn_apply_ao,
                      self.chk_do0, self.chk_do1, self.btn_pulse):
                w.setEnabled(False)

    # ---------------------- UI Builders ---------------------- #

    def _pillify(self, label: QLabel, bg: str, fg: str):
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(f"QLabel{{background:{bg}; color:{fg}; padding:4px 8px; border-radius:8px; font:10pt 'Segoe UI';}}")

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

    def _build_ao_group(self) -> QGroupBox:
        gb = QGroupBox("Electrode Voltages (AO)")
        form = QFormLayout(gb); form.setLabelAlignment(Qt.AlignRight); form.setContentsMargins(8,8,8,8); form.setSpacing(6)
        self.sp_ao0 = QDoubleSpinBox(); self._sty_spin(self.sp_ao0, -10.0, 10.0, 0.01, 0.00); self.sp_ao0.setSuffix(" V")
        self.sp_ao1 = QDoubleSpinBox(); self._sty_spin(self.sp_ao1, -10.0, 10.0, 0.01, 0.00); self.sp_ao1.setSuffix(" V")
        self.sp_ramp = QSpinBox(); self._sty_spin(self.sp_ramp, 1, 2000, 1, 100); self.sp_ramp.setSuffix(" ms")
        self.btn_apply_ao = QPushButton("Apply"); self._sty_btn(self.btn_apply_ao); self.btn_apply_ao.clicked.connect(self._apply_ao_clicked)
        form.addRow("AO0 (Endcap A):", self.sp_ao0); form.addRow("AO1 (Endcap B):", self.sp_ao1); form.addRow("Ramp time:", self.sp_ramp); form.addRow("", self.btn_apply_ao)
        self.gb_ao = gb; return gb

    def _build_do_group(self) -> QGroupBox:
        gb = QGroupBox("Digital Lines (DO)")
        lay = QGridLayout(gb); lay.setContentsMargins(8,8,8,8); lay.setSpacing(6)
        self.chk_do0 = QCheckBox("port0/line0"); self._sty_chk(self.chk_do0)
        self.chk_do1 = QCheckBox("port0/line1"); self._sty_chk(self.chk_do1)
        self.chk_do0.stateChanged.connect(lambda s: self._write_do("port0/line0", s == Qt.Checked))
        self.chk_do1.stateChanged.connect(lambda s: self._write_do("port0/line1", s == Qt.Checked))
        self.btn_pulse = QPushButton("Pulse line0 (50 ms)"); self._sty_btn(self.btn_pulse); self.btn_pulse.clicked.connect(lambda: self._pulse_do("port0/line0", 50))
        lay.addWidget(self.chk_do0, 0, 0); lay.addWidget(self.chk_do1, 0, 1); lay.addWidget(self.btn_pulse, 1, 0, 1, 2)
        self.gb_do = gb; return gb

    def _build_acq_group(self) -> QGroupBox:
        gb = QGroupBox("Acquisition"); lay = QGridLayout(gb); lay.setContentsMargins(8,8,8,8); lay.setSpacing(6)

        # Source selector
        self.cb_source = QComboBox(); self.cb_source.addItems(["Synthetic", "PicoScope (Rapid)", "PicoScope (Streaming)"])
        self.cb_source.currentIndexChanged.connect(self._on_source_changed)

        # Parameters (shared for now; Pico can interpret as its own fs/N later)
        self.chk_synth = QCheckBox("Use synthetic generator"); self.chk_synth.setChecked(True); self._sty_chk(self.chk_synth)
        self.sp_fs = QDoubleSpinBox(); self._sty_spin(self.sp_fs, 100_000, 5_000_000, 1_000, 2_400_000); self.sp_fs.setSuffix(" Hz")
        self.sp_N  = QSpinBox();      self._sty_spin(self.sp_N, 16_384, 1_048_576, 1024, 262_144)
        self.sp_period = QSpinBox();  self._sty_spin(self.sp_period, 10, 2000, 10, 250); self.sp_period.setSuffix(" ms")
        self.btn_start = QPushButton("Start"); self._sty_btn(self.btn_start)
        self.btn_stop  = QPushButton("Stop");  self._sty_btn(self.btn_stop); self.btn_stop.setEnabled(False)
        self.lbl_src_hint = QLabel(""); self.lbl_src_hint.setStyleSheet("font:9pt 'Segoe UI';")

        self.btn_start.clicked.connect(self._start_clicked); self.btn_stop.clicked.connect(self._stop_clicked)

        lay.addWidget(QLabel("Source:"), 0, 0); lay.addWidget(self.cb_source, 0, 1)
        lay.addWidget(self.chk_synth, 1, 0, 1, 2)
        lay.addWidget(QLabel("Sample rate:"), 2, 0); lay.addWidget(self.sp_fs, 2, 1)
        lay.addWidget(QLabel("Samples per event:"), 3, 0); lay.addWidget(self.sp_N, 3, 1)
        lay.addWidget(QLabel("Event period:"), 4, 0); lay.addWidget(self.sp_period, 4, 1)
        lay.addWidget(self.btn_start, 5, 0); lay.addWidget(self.btn_stop, 5, 1)
        lay.addWidget(self.lbl_src_hint, 6, 0, 1, 2)
        # Initial hint
        self._on_source_changed()
        return gb

    def _build_table(self) -> QTableWidget:
        tbl = QTableWidget(0, 6)
        tbl.setHorizontalHeaderLabels(["Time", "Class", "f0 (kHz)", "SNR (dB)", "#Peaks", "Notes"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        tbl.setStyleSheet(
            f"QTableWidget{{background:{style.CARD_BG}; color:{TXT_FG}; gridline-color:{style.CARD_BORDER};}}"
            f"QHeaderView::section{{background:{style.BTN_BG}; color:{TXT_FG}; border:1px solid {style.BTN_BORDER}; padding:4px; font-weight:600;}}"
        )
        return tbl

    def _build_hist_plot(self) -> QWidget:
        pg.setConfigOptions(background=style.BG, foreground=TXT_FG)
        w = pg.PlotWidget(); w.setLabel('left', 'Count'); w.setLabel('bottom', 'f0 (kHz)'); w.showGrid(x=True, y=True, alpha=0.25)
        self._hist_curve = pg.BarGraphItem(x=[], height=[], width=1.0); w.addItem(self._hist_curve)
        return w

    # ---------------------- Handlers ---------------------- #

    def _on_source_changed(self):
        src = self.cb_source.currentText()
        if src == "Synthetic":
            self.chk_synth.setEnabled(True)
            self.lbl_src_hint.setText("Synthetic demo mode — no hardware required.")
            self.btn_start.setEnabled(True)
        else:
            self.chk_synth.setChecked(False); self.chk_synth.setEnabled(False)
            if not HAVE_PICO:
                self.lbl_src_hint.setText("PicoScope selected, but PicoSDK/service not installed. (Synthetic still works.)")
            else:
                self.lbl_src_hint.setText("PicoScope mode — configure scope params in the future scope panel.")
            self.btn_start.setEnabled(HAVE_PICO)

    def _apply_ao_clicked(self):
        if self.daq is None: return
        ramp = int(self.sp_ramp.value())
        self.daq.set_voltage("ao0", float(self.sp_ao0.value()), ramp)
        self.daq.set_voltage("ao1", float(self.sp_ao1.value()), ramp)

    def _write_do(self, line: str, level: bool):
        if self.daq is None: return
        self.daq.write_do(line, level)

    def _pulse_do(self, line: str, width_ms: int):
        if self.daq is None: return
        self.daq.write_do(line, True)
        QTimer.singleShot(width_ms, lambda: self.daq.write_do(line, False))

    def _start_clicked(self):
        src = self.cb_source.currentText()

        # Reset counters/plots
        self._events_seen = 0; self._counts = {"no_ion":0,"single":0,"multiple":0}; self._f0_hist_vals.clear()
        self._update_counters()

        if src == "Synthetic":
            self.gen.fs = float(self.sp_fs.value()); self.gen.N = int(self.sp_N.value()); self.gen.period_ms = int(self.sp_period.value())
            if not self.gen_thread.isRunning():
                self.gen_thread.started.connect(self.gen.start, Qt.QueuedConnection)
                self.gen.block_ready.connect(self.rt.analyze_block, Qt.QueuedConnection)
                self.gen_thread.start()
        else:
            if not HAVE_PICO:
                QMessageBox.information(self, "PicoScope not available", "Install PicoSDK + add services/scope_pico.py to enable Pico sources.\nUsing Synthetic for now.")
                return
            # Create scope service on demand
            if self.pico_thread is None:
                self.pico_thread = QThread(); self.pico = PicoScopeService()
                self.pico.moveToThread(self.pico_thread)
                self.pico.block_ready.connect(self.rt.analyze_block, Qt.QueuedConnection)
                self.pico_thread.start()
            # Start appropriate mode
            if "Rapid" in src:
                QTimer.singleShot(0, self.pico.start_rapid_block)
            else:
                QTimer.singleShot(0, self.pico.start_streaming)

        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)

    def _stop_clicked(self):
        # Stop synthetic
        if self.gen_thread.isRunning():
            self.gen.stop(); QThread.msleep(20)
            self.gen_thread.quit(); self.gen_thread.wait()
            try: self.gen_thread.started.disconnect(self.gen.start)
            except Exception: pass

        # Stop Pico (if created)
        if self.pico and self.pico_thread and self.pico_thread.isRunning():
            try: self.pico.stop()
            except Exception: pass
            self.pico_thread.quit(); self.pico_thread.wait()

        self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)

    @pyqtSlot(object)
    def _on_event_result(self, res: EventResult):
        self._events_seen += 1; self._counts[res.cls] = self._counts.get(res.cls, 0) + 1
        # table append (cap at 500)
        dt = QDateTime.fromMSecsSinceEpoch(int(res.timestamp * 1000)).toString("hh:mm:ss.zzz")
        row = self.table.rowCount(); self.table.insertRow(row)
        def setc(col, text):
            it = QTableWidgetItem(text); it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable); self.table.setItem(row, col, it)
        setc(0, dt); setc(1, res.cls)
        setc(2, f"{(res.f0_hz or 0.0)/1000.0:,.1f}" if res.f0_hz else "-")
        setc(3, f"{res.snr_db:.1f}" if res.snr_db is not None else "-")
        setc(4, str(res.n_peaks)); setc(5, "")
        if self.table.rowCount() > 500: self.table.removeRow(0)

        if res.cls == "single" and res.f0_hz:
            self._f0_hist_vals.append(res.f0_hz / 1000.0)
            if len(self._f0_hist_vals) % 5 == 0: self._refresh_hist()

        self._update_counters()

    def _update_counters(self):
        self.lbl_empty.setText(f"Empty: {self._counts['no_ion']}")
        self.lbl_single.setText(f"Single: {self._counts['single']}")
        self.lbl_multi.setText(f"Multiple: {self._counts['multiple']}")

    def _update_rate(self):
        now = time.time(); dt = max(now - self._t0, 1e-3)
        rate = (self._events_seen - self._last_n) / dt
        self.lbl_rate.setText(f"Rate: {rate:,.1f} evt/s")
        self._t0 = now; self._last_n = self._events_seen

    def _refresh_hist(self):
        if not self._f0_hist_vals:
            self._hist_curve.setOpts(x=[], height=[], width=1.0); return
        vals = np.array(self._f0_hist_vals, dtype=np.float32)
        vmin, vmax = float(np.min(vals)), float(np.max(vals))
        if vmin == vmax: vmin -= 0.5; vmax += 0.5
        bins = max(20, min(80, int(max(10.0, (vmax - vmin)))))  # ~20-80 bars
        hist, edges = np.histogram(vals, bins=bins, range=(vmin, vmax))
        x = (edges[:-1] + edges[1:]) * 0.5
        self._hist_curve.setOpts(x=x, height=hist, width=(edges[1]-edges[0]) * 0.9)

    # ---------------------- Qt lifecycle ---------------------- #
    def closeEvent(self, ev):
        self._stop_clicked()
        super().closeEvent(ev)
