"""
Processing Page: Calibrated ion extraction and visualization.

Responsibilities:
- Let the user pick a calibration JSON and set default V/E.
- Subscribe to a background scope worker (PicoScope) that emits frames.
- Call processing.geo_calibration.process_frame(...) in a worker thread.
- Render a live mass (m/z by default) histogram and throughput stats.
- Provide Export CSV of the current histogram and a simple per-ion snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List
from pathlib import Path

import numpy as np
from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal, pyqtSlot, QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QLabel,
    QFileDialog, QDoubleSpinBox, QGroupBox, QMessageBox
)
import pyqtgraph as pg

from instrument_app.processing.geo_calibration import (
    load_calibration, configure_processing, process_frame,
    HistState, update_hist, export_hist, Ion, CDMSCalibration,
)

# Optional Pico import (graceful fallback)
HAVE_PICO = False
try:
    from instrument_app.services.scope_pico import PicoScopeService
    HAVE_PICO = True
except Exception:  # pragma: no cover - optional dependency
    PicoScopeService = None  # type: ignore


class ProcessingWorker(QObject):
    ions_ready = pyqtSignal(object)  # List[Ion]
    status = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._calib: Optional[CDMSCalibration] = None
        self._E_default: float = 1_000.0  # eV/z
        self._V_default: Optional[float] = None
        self._configured = False

    @pyqtSlot(object)
    def set_calibration(self, calib: object):
        self._calib = calib  # CDMSCalibration
        self.status.emit("Calibration set")

    @pyqtSlot(float)
    def set_E_default(self, E: float):
        self._E_default = float(E)

    @pyqtSlot(float)
    def set_V_default(self, V: float):
        self._V_default = float(V)

    @pyqtSlot(object, float)
    def process_block(self, x_i16: object, fs_hz: float):
        if self._calib is None:
            self.status.emit("No calibration set; dropping frame.")
            return
        if not self._configured:
            # Initialize processing configuration with actual sample rate
            configure_processing(fs_hz=float(fs_hz))
            self._configured = True
            self.status.emit(f"Processing configured: fs={fs_hz:,.0f} Hz")
        try:
            x16 = np.asarray(x_i16, dtype=np.int16)
            x = x16.astype(np.float32)
            ions: List[Ion] = process_frame(x, self._E_default, self._V_default,
                                            calib=self._calib, reject_overlap_hz=None)
            if ions:
                self.ions_ready.emit(ions)
        except Exception as e:
            self.status.emit(f"Processing error: {e}")


class ProcessingPage(QWidget):
    """Data processing/visualization tab (calibrated outputs)."""

    def __init__(self):
        super().__init__()
        self._calib: Optional[CDMSCalibration] = None
        self._hist = HistState(field_name="mz", bins=120)
        self._ions_seen = 0
        self._events_seen = 0
        self._last_events = 0
        self._t0 = None

        self._pico: Optional[PicoScopeService] = None
        self._pico_thread: Optional[QThread] = None
        self._worker = ProcessingWorker()
        self._worker_thread = QThread(); self._worker.moveToThread(self._worker_thread)
        self._worker_thread.start()

        self._build_ui()
        self._wire_signals()

        # rate timer
        self._rate_timer = QTimer(self); self._rate_timer.setInterval(1000)
        self._rate_timer.timeout.connect(self._update_rate)

    # ---------------- UI -----------------
    def _build_ui(self):
        grid = QGridLayout(self); grid.setContentsMargins(10, 8, 10, 10); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(10)

        # Controls group
        gb = QGroupBox("Calibration && Defaults"); grid.addWidget(gb, 0, 0, 1, 2)
        row = QHBoxLayout(gb); row.setSpacing(8)
        self.btn_browse = QPushButton("Load Calibration…"); self.btn_browse.setFixedHeight(32)
        self.lbl_calib = QLabel("No calibration loaded")
        self.sp_E = QDoubleSpinBox(); self.sp_E.setDecimals(1); self.sp_E.setRange(0.0, 1e9); self.sp_E.setValue(1000.0); self.sp_E.setSuffix(" eV/z"); self.sp_E.setFixedHeight(32)
        self.sp_V = QDoubleSpinBox(); self.sp_V.setDecimals(2); self.sp_V.setRange(0.0, 1e6); self.sp_V.setValue(0.0); self.sp_V.setSuffix(" V"); self.sp_V.setFixedHeight(32)
        self.btn_start = QPushButton("Start"); self.btn_start.setFixedHeight(32)
        self.btn_stop = QPushButton("Stop"); self.btn_stop.setFixedHeight(32); self.btn_stop.setEnabled(False)
        row.addWidget(self.btn_browse); row.addWidget(self.lbl_calib, 1)
        row.addStretch(1)
        row.addWidget(QLabel("E default:")); row.addWidget(self.sp_E)
        row.addWidget(QLabel("V default:")); row.addWidget(self.sp_V)
        row.addStretch(1)
        row.addWidget(self.btn_start); row.addWidget(self.btn_stop)

        # Plot area
        self.plot = pg.PlotWidget(); grid.addWidget(self.plot, 1, 0, 1, 2)
        self.plot.setLabel('left', 'Count'); self.plot.setLabel('bottom', 'm/z')
        self._bars = pg.BarGraphItem(x=[], height=[], width=1.0)
        self.plot.addItem(self._bars)

        # Footer actions + stats
        foot = QHBoxLayout(); foot.setSpacing(8); grid.addLayout(foot, 2, 0, 1, 2)
        self.btn_export = QPushButton("Export Histogram CSV…"); self.btn_export.setFixedHeight(30)
        self.btn_snapshot = QPushButton("Save Ion Snapshot CSV…"); self.btn_snapshot.setFixedHeight(30)
        foot.addWidget(self.btn_export); foot.addWidget(self.btn_snapshot); foot.addStretch(1)
        self.lbl_events = QLabel("Events: 0"); self.lbl_ions = QLabel("Ions: 0"); self.lbl_rate = QLabel("Rate: 0.0 evt/s")
        foot.addWidget(self.lbl_events); foot.addWidget(self.lbl_ions); foot.addWidget(self.lbl_rate)

    def _wire_signals(self):
        self.btn_browse.clicked.connect(self._choose_calibration)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_export.clicked.connect(self._export_hist)
        self.btn_snapshot.clicked.connect(self._export_snapshot)
        # worker updates
        self._worker.ions_ready.connect(self._on_ions_ready)

    # --------------- actions ---------------
    def _choose_calibration(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open Calibration JSON", str(Path.home()), "JSON (*.json)")
        if not p:
            return
        try:
            calib = load_calibration(p)
            self._calib = calib
            self.lbl_calib.setText(Path(p).name)
            # Default V from calibration if provided
            if calib.voltage_median:
                try:
                    self.sp_V.setValue(float(calib.voltage_median))
                except Exception:
                    pass
            # push to worker
            self._worker.set_calibration(calib)
        except Exception as e:
            QMessageBox.warning(self, "Calibration", f"Failed to load: {e}")

    def _start(self):
        if not HAVE_PICO:
            QMessageBox.information(self, "PicoScope", "PicoSDK/service not installed.")
            return
        if self._calib is None:
            QMessageBox.information(self, "Calibration", "Load a calibration JSON first.")
            return
        # update defaults
        self._worker.set_E_default(self.sp_E.value())
        self._worker.set_V_default(self.sp_V.value())
        # start pico thread if not running
        if self._pico_thread is None:
            self._pico_thread = QThread(); self._pico = PicoScopeService()
            self._pico.moveToThread(self._pico_thread)
            # Connect scope -> worker (queued cross-thread)
            self._pico.block_ready.connect(self._worker.process_block, Qt.QueuedConnection)
            self._pico_thread.start()
        QTimer.singleShot(0, self._pico.start_rapid_block)
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self._rate_timer.start()
        self._events_seen = 0; self._ions_seen = 0; self._last_events = 0
        self._refresh_stats()

    def _stop(self):
        if self._pico and self._pico_thread and self._pico_thread.isRunning():
            try: self._pico.stop()
            except Exception: pass
            self._pico_thread.quit(); self._pico_thread.wait()
        self._pico = None; self._pico_thread = None
        self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)
        self._rate_timer.stop(); self.lbl_rate.setText("Rate: 0.0 evt/s")

    @pyqtSlot(object)
    def _on_ions_ready(self, ions: object):  # List[Ion]
        lst: List[Ion] = list(ions) if isinstance(ions, list) else ions
        self._events_seen += 1
        self._ions_seen += len(lst)
        update_hist(self._hist, lst)
        self._refresh_hist_plot()
        self._refresh_stats()

    def _refresh_hist_plot(self):
        if self._hist.edges is None or self._hist.counts is None:
            self._bars.setOpts(x=[], height=[], width=1.0); return
        edges = self._hist.edges; counts = self._hist.counts
        x = (edges[:-1] + edges[1:]) * 0.5
        w = (edges[1] - edges[0]) * 0.9 if len(edges) > 1 else 1.0
        self._bars.setOpts(x=x, height=counts, width=w)

    def _refresh_stats(self):
        self.lbl_events.setText(f"Events: {self._events_seen}")
        self.lbl_ions.setText(f"Ions: {self._ions_seen}")

    def _update_rate(self):
        de = self._events_seen - self._last_events
        self._last_events = self._events_seen
        self.lbl_rate.setText(f"Rate: {de:.1f} evt/s")

    def _export_hist(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export Histogram CSV", str(Path.home() / "hist.csv"), "CSV (*.csv)")
        if not p:
            return
        try:
            export_hist(self._hist, p)
        except Exception as e:
            QMessageBox.warning(self, "Export", f"Failed to export: {e}")

    def _export_snapshot(self):
        # No per-ion cache here; offer exporting the current histogram as a snapshot
        self._export_hist()

    def closeEvent(self, ev):
        try:
            self._stop()
            if self._worker_thread and self._worker_thread.isRunning():
                self._worker_thread.quit(); self._worker_thread.wait()
        finally:
            super().closeEvent(ev)

