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
    build_charge_cal_from_calibration,
)
from instrument_app.core.bus import bus
from instrument_app.core.app_context import ctx

# Optional Pico import (graceful fallback)
HAVE_PICO = False
try:
    from instrument_app.services.scope_pico import PicoScopeService
    HAVE_PICO = True
except Exception:  # pragma: no cover - optional dependency
    PicoScopeService = None  # type: ignore


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

        # central pipeline: subscribe to bus emissions

        self._build_ui()
        self._wire_signals()
        self._load_default_calibration()

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
        # subscribe to bus
        bus.ions_batch.connect(self._on_ions_ready)
        bus.status.connect(lambda s: None)  # could surface in UI later

    def _load_default_calibration(self):
        # Attempt to load repository-bundled calibration
        default_path = Path(__file__).resolve().parent.parent / "processing" / "calibration_files" / "SIMION_Calibration.json"
        if default_path.exists():
            try:
                calib = load_calibration(str(default_path))
                self._set_active_calibration(calib, default_path)
            except Exception as e:
                # non-fatal; UI remains usable for manual load
                pass

    # --------------- actions ---------------
    def _choose_calibration(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open Calibration JSON", str(Path.home()), "JSON (*.json)")
        if not p:
            return
        try:
            calib = load_calibration(p)
            self._set_active_calibration(calib, Path(p))
        except Exception as e:
            QMessageBox.warning(self, "Calibration", f"Failed to load: {e}")

    def _set_active_calibration(self, calib: CDMSCalibration, path: Path) -> None:
        self._calib = calib
        # Show file name and key metadata
        meta_bits = []
        if calib.voltage_median is not None:
            meta_bits.append(f"Vmed={float(calib.voltage_median):.2f} V")
        if getattr(calib, 'r2', None) is not None:
            try:
                meta_bits.append(f"R²={float(calib.r2):.4f}")
            except Exception:
                pass
        suffix = (" (" + ", ".join(meta_bits) + ")") if meta_bits else ""
        self.lbl_calib.setText(f"{path.name}{suffix}")
        # Default V from calibration if provided
        if calib.voltage_median:
            try:
                self.sp_V.setValue(float(calib.voltage_median))
            except Exception:
                pass
        # push to central processor
        ctx.processor.set_calibration(calib)

    def _start(self):
        if self._calib is None:
            QMessageBox.information(self, "Calibration", "Load a calibration JSON first.")
            return
        # update defaults
        ctx.processor.set_E_default(self.sp_E.value())
        ctx.processor.set_V_default(self.sp_V.value())
        # start source via SourceManager (Pico Rapid for now)
        try:
            ctx.sources.start_pico_rapid()
        except Exception as e:
            QMessageBox.information(self, "Source", f"Failed to start Pico: {e}")
            return
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self._rate_timer.start()
        self._events_seen = 0; self._ions_seen = 0; self._last_events = 0
        self._refresh_stats()

    def _stop(self):
        ctx.sources.stop()
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
