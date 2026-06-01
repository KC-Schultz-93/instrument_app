"""
daq_page.py
-----------
DAQ GUI page for PicoScope 4262 waveform acquisition.

Layout
------
Horizontal splitter:
  Left panel  (fixed 300 px) — connection, config, start/stop, status/counters
  Right panel (expandable)   — live waveform plot

Threading model
---------------
Acquisition runs in AcquisitionWorker (QThread). The worker emits signals
that are received here on the main Qt thread. The plot is throttled to
~10 Hz to prevent UI lag during fast acquisitions.

Follows the same style conventions as Pages/pressure_page.py:
  - style.* tokens for colours
  - pyqtgraph PlotWidget with grid, pen styling
  - QSplitter for resizable panels
  - QGroupBox for logical sections
"""

from __future__ import annotations

import dataclasses
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from Services.AcquisitionWorker import AcquisitionWorker
from Services.Channels import AppChannels
from Services.DAQLogger import DAQLogger
from Services.DAQModels import (
    AcquisitionConfig,
    CDMSConfig,
    CDMSResult,
    EventSummary,
    STFTResult,
    WaveformRecord,
)
from Services.PicoScopeService import PicoScopeService
from Services.WaveformProcessor import WaveformProcessor
from UI.theme import style


# Voltage range options (display label → float value in volts)
_VOLTAGE_RANGES = {
    "±10 mV":  0.01,
    "±20 mV":  0.02,
    "±50 mV":  0.05,
    "±100 mV": 0.1,
    "±200 mV": 0.2,
    "±500 mV": 0.5,
    "±1 V":    1.0,
    "±2 V":    2.0,
    "±5 V":    5.0,
    "±10 V":   10.0,
}

# Plot update rate cap (seconds between plot refreshes)
_PLOT_MIN_INTERVAL_S = 0.1  # 10 Hz


class DAQPage(QWidget):
    """DAQ acquisition and display page."""

    def __init__(self, channels: AppChannels, parent=None) -> None:
        super().__init__(parent)

        self.channels = channels
        self._service = PicoScopeService()
        self._worker: Optional[AcquisitionWorker] = None
        self._logger: Optional[DAQLogger] = None
        self._run_id: Optional[str] = None

        # Counters (reset on each run start)
        self._total_traces: int = 0
        self._accepted_traces: int = 0

        # Plot throttle (shared for both waveform and FFT panels)
        self._last_plot_update: float = 0.0

        # Last FFT dominant frequency (kept for summary text)
        self._last_dominant_freq_hz: float = 0.0

        # Last STFT result (stored for display when cdms_ready fires)
        self._last_stft: Optional[STFTResult] = None
        self._last_cdms: Optional[CDMSResult] = None
        # Separate throttle clock for STFT (shares the same 10 Hz cap)
        self._last_stft_update: float = 0.0

        # Build UI
        self._build_ui()
        self._connect_channels()
        self._set_controls_idle()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._make_left_panel())
        splitter.addWidget(self._make_plot_panel())
        splitter.setSizes([300, 900])
        splitter.setChildrenCollapsible(False)

        root.addWidget(splitter)

    def _make_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(300)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(8)

        layout.addWidget(self._make_connection_group())
        layout.addWidget(self._make_config_group())
        layout.addWidget(self._make_cdms_calibration_group())
        layout.addWidget(self._make_control_group())
        layout.addWidget(self._make_status_group())
        layout.addStretch()

        return panel

    def _make_connection_group(self) -> QGroupBox:
        box = QGroupBox("PicoScope Connection")
        lay = QVBoxLayout(box)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._on_connect_clicked)

        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.clicked.connect(self._on_disconnect_clicked)

        self.lbl_connection = QLabel("Disconnected")
        self.lbl_connection.setAlignment(Qt.AlignCenter)
        self._set_label_bad(self.lbl_connection, "Disconnected")

        lay.addWidget(self.btn_connect)
        lay.addWidget(self.btn_disconnect)
        lay.addWidget(self.lbl_connection)
        return box

    def _make_config_group(self) -> QGroupBox:
        box = QGroupBox("Acquisition Settings")
        lay = QVBoxLayout(box)

        # Channel
        lay.addWidget(QLabel("Channel:"))
        self.combo_channel = QComboBox()
        self.combo_channel.addItems(["A", "B"])
        lay.addWidget(self.combo_channel)

        # Voltage range
        lay.addWidget(QLabel("Voltage range:"))
        self.combo_range = QComboBox()
        for label in _VOLTAGE_RANGES:
            self.combo_range.addItem(label)
        self.combo_range.setCurrentText("±2 V")
        lay.addWidget(self.combo_range)

        # Coupling
        lay.addWidget(QLabel("Coupling:"))
        self.combo_coupling = QComboBox()
        self.combo_coupling.addItems(["DC", "AC"])
        lay.addWidget(self.combo_coupling)

        # Sample interval
        lay.addWidget(QLabel("Sample interval (ns):"))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(10, 1_000_000)
        self.spin_interval.setValue(200)
        self.spin_interval.setSingleStep(10)
        lay.addWidget(self.spin_interval)

        # Num samples
        lay.addWidget(QLabel("Samples per trace:"))
        self.spin_samples = QSpinBox()
        self.spin_samples.setRange(100, 1_000_000)
        self.spin_samples.setValue(5000)
        self.spin_samples.setSingleStep(500)
        lay.addWidget(self.spin_samples)

        # Invert polarity
        self.chk_invert = QCheckBox("Invert polarity")
        lay.addWidget(self.chk_invert)

        return box

    def _make_cdms_calibration_group(self) -> QGroupBox:
        box = QGroupBox("CDMS Calibration")
        lay = QVBoxLayout(box)

        lay.addWidget(QLabel("Charge cal (µV/e):"))
        self.spin_charge_cal = QDoubleSpinBox()
        self.spin_charge_cal.setRange(0.01, 10.0)
        self.spin_charge_cal.setValue(0.64)
        self.spin_charge_cal.setSingleStep(0.01)
        self.spin_charge_cal.setDecimals(2)
        self.spin_charge_cal.setToolTip(
            "CoolFET amplifier calibration:\nQ [e] = V [µV] / cal_factor"
        )
        lay.addWidget(self.spin_charge_cal)

        lay.addWidget(QLabel("Trap K (Da·Hz²):"))
        self.edit_trap_k = QLineEdit("0")
        self.edit_trap_k.setPlaceholderText("0 = disabled (uncalibrated)")
        self.edit_trap_k.setToolTip(
            "Simple harmonic trap constant:\nm/z [Da/e] = K / f [Hz]²\n"
            "Set to 0 until calibrated with SIMION."
        )
        lay.addWidget(self.edit_trap_k)

        lay.addWidget(QLabel("STFT window (samples):"))
        self.combo_stft_window = QComboBox()
        for n in ["64", "128", "256", "512", "1024", "2048"]:
            self.combo_stft_window.addItem(n)
        self.combo_stft_window.setCurrentText("512")
        self.combo_stft_window.setToolTip(
            "Samples per STFT window. Larger = better frequency resolution, "
            "worse time resolution."
        )
        lay.addWidget(self.combo_stft_window)

        return box

    def _make_control_group(self) -> QGroupBox:
        box = QGroupBox("Acquisition Control")
        lay = QVBoxLayout(box)

        self.btn_start = QPushButton("Start Acquisition")
        self.btn_start.clicked.connect(self._on_start_clicked)

        self.btn_stop = QPushButton("Stop Acquisition")
        self.btn_stop.clicked.connect(self._on_stop_clicked)

        self.lbl_run_status = QLabel("Idle")
        self.lbl_run_status.setAlignment(Qt.AlignCenter)

        lay.addWidget(self.btn_start)
        lay.addWidget(self.btn_stop)
        lay.addWidget(self.lbl_run_status)
        return box

    def _make_status_group(self) -> QGroupBox:
        box = QGroupBox("Event Status")
        lay = QVBoxLayout(box)

        self.lbl_trace_count = QLabel("Traces:  0")
        self.lbl_accepted_count = QLabel("Accepted:  0 / 0")
        self.lbl_classification = QLabel("Last class:  —")

        for lbl in (self.lbl_trace_count, self.lbl_accepted_count, self.lbl_classification):
            lay.addWidget(lbl)

        lay.addWidget(QLabel("Last event summary:"))
        self.text_summary = QTextEdit()
        self.text_summary.setReadOnly(True)
        self.text_summary.setFixedHeight(130)
        self.text_summary.setStyleSheet(
            f"background: {style.CARD_BG}; color: {style.TXT}; font-size: 11px;"
        )
        lay.addWidget(self.text_summary)
        return box

    def _make_plot_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(0)

        vsplit = QSplitter(Qt.Vertical)

        # --- Time-domain waveform (top) ---
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground(style.PLOT_BG)
        self.plot_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        pi = self.plot_widget.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.5)
        pi.setLabel("bottom", "Time", units="µs")
        pi.setLabel("left", "Voltage", units="V")
        pi.setTitle("Waveform")

        axis_pen = pg.mkPen(color=style.BTN_BORDER)
        for ax in ("bottom", "left"):
            pi.getAxis(ax).setPen(axis_pen)
            pi.getAxis(ax).setTextPen(style.TXT)

        self._plot_item = self.plot_widget.plot(
            [], [], pen=pg.mkPen(color=style.GOOD, width=1),
        )

        vsplit.addWidget(self.plot_widget)

        # --- Frequency-domain FFT spectrum (bottom) ---
        fft_container = QWidget()
        fft_lay = QVBoxLayout(fft_container)
        fft_lay.setContentsMargins(0, 4, 0, 0)
        fft_lay.setSpacing(2)

        self.fft_widget = pg.PlotWidget()
        self.fft_widget.setBackground(style.PLOT_BG)
        self.fft_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        fpi = self.fft_widget.getPlotItem()
        fpi.showGrid(x=True, y=True, alpha=0.5)
        fpi.setLabel("bottom", "Frequency", units="kHz")
        fpi.setLabel("left", "Magnitude", units="dBV")
        fpi.setTitle("FFT Amplitude Spectrum")

        for ax in ("bottom", "left"):
            fpi.getAxis(ax).setPen(axis_pen)
            fpi.getAxis(ax).setTextPen(style.TXT)

        self._fft_plot_item = self.fft_widget.plot(
            [], [], pen=pg.mkPen(color=style.BAD, width=1),
        )

        self.lbl_dominant_freq = QLabel("Dominant frequency:  —")
        self.lbl_dominant_freq.setStyleSheet(
            f"color: {style.TXT}; font-size: 11px; padding: 2px 6px;"
        )

        fft_lay.addWidget(self.fft_widget)
        fft_lay.addWidget(self.lbl_dominant_freq)

        vsplit.addWidget(fft_container)

        # --- STFT spectrogram (bottom) ---
        stft_container = QWidget()
        stft_lay = QVBoxLayout(stft_container)
        stft_lay.setContentsMargins(0, 4, 0, 0)
        stft_lay.setSpacing(2)

        self.stft_widget = pg.PlotWidget()
        self.stft_widget.setBackground(style.PLOT_BG)
        self.stft_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        spi = self.stft_widget.getPlotItem()
        spi.showGrid(x=True, y=True, alpha=0.3)
        spi.setLabel("bottom", "Time", units="µs")
        spi.setLabel("left", "Frequency", units="kHz")
        spi.setTitle("STFT Spectrogram")

        for ax in ("bottom", "left"):
            spi.getAxis(ax).setPen(axis_pen)
            spi.getAxis(ax).setTextPen(style.TXT)

        # ImageItem for the 2D power map
        self._stft_img = pg.ImageItem()
        try:
            cm = pg.colormap.get("plasma", source="matplotlib")
            self._stft_img.setColorMap(cm)
        except Exception:
            pass  # fall back to greyscale if matplotlib not available
        self.stft_widget.addItem(self._stft_img)

        # Overlay: dominant frequency track in white
        self._stft_dom_curve = pg.PlotCurveItem(
            pen=pg.mkPen(color="w", width=1.5)
        )
        self.stft_widget.addItem(self._stft_dom_curve)

        self.lbl_stft_info = QLabel("STFT: awaiting data")
        self.lbl_stft_info.setStyleSheet(
            f"color: {style.TXT}; font-size: 11px; padding: 2px 6px;"
        )

        stft_lay.addWidget(self.stft_widget)
        stft_lay.addWidget(self.lbl_stft_info)

        vsplit.addWidget(stft_container)
        vsplit.setSizes([320, 200, 200])

        lay.addWidget(vsplit)
        return panel

    # ------------------------------------------------------------------
    # Channel signal connections
    # ------------------------------------------------------------------

    def _connect_channels(self) -> None:
        self.channels.daq_connected.connect(self._on_daq_connected)
        self.channels.daq_status.connect(self._on_daq_status)
        self.channels.daq_error.connect(self._on_daq_error)

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_connect_clicked(self) -> None:
        try:
            self._service.connect()
            self.channels.daq_connected.emit(True)
        except Exception as exc:
            self.channels.daq_error.emit(f"Connect failed: {exc}")

    def _on_disconnect_clicked(self) -> None:
        self.stop_acquisition()
        try:
            self._service.disconnect()
        except Exception as exc:
            self.channels.daq_error.emit(f"Disconnect error: {exc}")
        self.channels.daq_connected.emit(False)

    def _on_start_clicked(self) -> None:
        if not self._service.is_connected:
            self._on_daq_error("Not connected to PicoScope.")
            return
        if self._worker is not None:
            return  # already running

        config = self._build_config()
        cdms_config = self._build_cdms_config()
        run_id = DAQLogger.make_run_id()
        self._run_id = run_id

        base_dir = DAQLogger.default_base_dir()
        self._logger = DAQLogger(base_dir, run_id)
        self._logger.write_metadata({
            "run_id": run_id,
            "start_time": datetime.now().isoformat(),
            "config": dataclasses.asdict(config),
            "cdms_config": dataclasses.asdict(cdms_config),
        })

        # Configure hardware
        try:
            self._service.configure_channel(config)
            self._service.set_trigger(config)
        except Exception as exc:
            self._on_daq_error(f"Hardware config error: {exc}")
            self._logger.close()
            self._logger = None
            return

        # Reset counters
        self._total_traces = 0
        self._accepted_traces = 0
        self._update_counters()

        # Start worker
        self._worker = AcquisitionWorker(
            self._service, config, cdms_config, self._logger, run_id
        )
        self._worker.waveform_ready.connect(self._on_waveform_received)
        self._worker.event_ready.connect(self._on_event_summary_received)
        self._worker.cdms_ready.connect(self._on_cdms_received)
        self._worker.stft_ready.connect(self._on_stft_received)
        self._worker.error_occurred.connect(self._on_daq_error)
        self._worker.status_update.connect(self._on_daq_status)
        self._worker.trace_count_changed.connect(self._on_trace_count)
        self._worker.start()

        self.channels.daq_run_started.emit(run_id)
        self._set_controls_running()
        self.lbl_run_status.setText(f"Running  [{run_id}]")

    def _on_stop_clicked(self) -> None:
        self.stop_acquisition()

    # ------------------------------------------------------------------
    # Public stop method (also called from MainWindow.closeEvent)
    # ------------------------------------------------------------------

    def stop_acquisition(self) -> None:
        """Stop the worker gracefully. Safe to call even if not running."""
        if self._worker is None:
            return

        self._worker.request_stop()
        finished = self._worker.wait(5000)  # 5-second timeout
        if not finished:
            self._worker.terminate()
            self._worker.wait(1000)

        total = self._total_traces
        self._worker = None

        if self._logger is not None:
            self._logger.write_metadata({
                "run_id": self._run_id,
                "end_time": datetime.now().isoformat(),
                "total_traces": total,
                "accepted_traces": self._accepted_traces,
            })
            self._logger.close()
            self._logger = None

        if self._run_id:
            self.channels.daq_run_stopped.emit(self._run_id, total)

        self._set_controls_idle()
        self.lbl_run_status.setText("Stopped")

    # ------------------------------------------------------------------
    # Worker signal slots (main thread)
    # ------------------------------------------------------------------

    def _on_waveform_received(self, record: WaveformRecord) -> None:
        """Update the waveform and FFT plots (throttled to ~10 Hz)."""
        now = time.monotonic()
        if now - self._last_plot_update < _PLOT_MIN_INTERVAL_S:
            return
        self._last_plot_update = now

        # Time-domain plot
        time_us = record.time_ns / 1e3
        self._plot_item.setData(time_us, record.voltage)

        # Frequency-domain plot
        freqs_hz, mags_db, dom_hz, dom_db = WaveformProcessor.compute_fft(
            record.voltage, float(record.sample_interval_ns)
        )
        self._fft_plot_item.setData(freqs_hz / 1e3, mags_db)  # x-axis in kHz
        self._last_dominant_freq_hz = dom_hz

        if dom_hz >= 1e3:
            freq_label = f"{dom_hz / 1e3:.2f} kHz"
        else:
            freq_label = f"{dom_hz:.1f} Hz"
        self.lbl_dominant_freq.setText(
            f"Dominant frequency:  {freq_label}  ({dom_db:.1f} dBV)"
        )

    def _on_event_summary_received(self, summary: EventSummary) -> None:
        """Update event counters and summary text."""
        self._total_traces += 1
        if summary.accepted:
            self._accepted_traces += 1
        self._update_counters()
        self.lbl_classification.setText(f"Last class:  {summary.classification}")
        self._update_summary_text(summary)

    def _on_cdms_received(self, result: CDMSResult) -> None:
        """Update CDMS summary line in the status text (throttled via STFT)."""
        # The summary text is updated in _on_event_summary_received; we just
        # store the latest result so _update_summary_text can use it.
        self._last_cdms = result

    def _on_stft_received(self, stft: STFTResult) -> None:
        """Render the STFT spectrogram (throttled to ~10 Hz, independent clock)."""
        now = time.monotonic()
        if now - self._last_stft_update < _PLOT_MIN_INTERVAL_S:
            return
        self._last_stft_update = now

        self._last_stft = stft
        if stft.power_db.size == 0:
            return

        times_us = stft.times_s * 1e6
        freqs_khz = stft.frequencies_hz / 1e3

        # power_db shape: (F, T); ImageItem expects (T, F) for x=time, y=freq
        img_data = stft.power_db.T

        dt = float(times_us[1] - times_us[0]) if len(times_us) > 1 else 1.0
        df = float(freqs_khz[1] - freqs_khz[0]) if len(freqs_khz) > 1 else 1.0

        self._stft_img.setImage(
            img_data,
            rect=pg.QtCore.QRectF(
                float(times_us[0]),
                float(freqs_khz[0]),
                float(times_us[-1] - times_us[0]) + dt,
                float(freqs_khz[-1] - freqs_khz[0]) + df,
            ),
        )

        # Dominant frequency track overlay
        dom_khz = stft.dominant_freq_track_hz / 1e3
        self._stft_dom_curve.setData(times_us, dom_khz)

        n_windows = len(times_us)
        f_range = f"{freqs_khz[0]:.1f}–{freqs_khz[-1]:.0f} kHz"
        self.lbl_stft_info.setText(
            f"STFT: {n_windows} windows  |  {f_range}"
        )

    def _on_trace_count(self, count: int) -> None:
        self.lbl_trace_count.setText(f"Traces:  {count}")

    def _on_daq_connected(self, connected: bool) -> None:
        if connected:
            self._set_label_good(self.lbl_connection, "Connected")
        else:
            self._set_label_bad(self.lbl_connection, "Disconnected")
        self._set_controls_idle() if not connected else None

    def _on_daq_status(self, msg: str) -> None:
        self.lbl_run_status.setText(msg)

    def _on_daq_error(self, msg: str) -> None:
        self.lbl_run_status.setText(f"Error: {msg}")
        self._set_controls_idle()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_config(self) -> AcquisitionConfig:
        range_label = self.combo_range.currentText()
        voltage_range_v = _VOLTAGE_RANGES.get(range_label, 2.0)
        return AcquisitionConfig(
            channel=self.combo_channel.currentText(),
            voltage_range_v=voltage_range_v,
            coupling=self.combo_coupling.currentText(),
            sample_interval_ns=self.spin_interval.value(),
            num_samples=self.spin_samples.value(),
            invert_polarity=self.chk_invert.isChecked(),
        )

    def _build_cdms_config(self) -> CDMSConfig:
        charge_cal = self.spin_charge_cal.value()
        try:
            trap_k = float(self.edit_trap_k.text().strip() or "0")
        except ValueError:
            trap_k = 0.0
        stft_nperseg = int(self.combo_stft_window.currentText())
        return CDMSConfig(
            charge_cal_uv_per_e=charge_cal,
            trap_k_Da_Hz2=trap_k,
            stft_nperseg=stft_nperseg,
        )

    def _update_counters(self) -> None:
        self.lbl_accepted_count.setText(
            f"Accepted:  {self._accepted_traces} / {self._total_traces}"
        )

    def _update_summary_text(self, summary: EventSummary) -> None:
        lines = [
            f"Trace {summary.trace_id}  [{summary.classification}]",
            f"  Baseline: {summary.baseline_mean_v * 1e3:.2f} mV"
            f"  RMS: {summary.baseline_rms_v * 1e6:.1f} µV",
            f"  Signal max: {summary.signal_max_v * 1e3:.2f} mV",
            f"  Peaks: {summary.num_peaks}",
        ]
        if summary.mean_peak_height_v is not None:
            lines.append(
                f"  Mean peak height: {summary.mean_peak_height_v * 1e3:.2f} mV"
            )
        if summary.mean_peak_spacing_ns is not None:
            lines.append(
                f"  Mean peak spacing: {summary.mean_peak_spacing_ns / 1e3:.2f} µs"
            )
        # CDMS physics
        if summary.oscillation_freq_hz is not None and summary.oscillation_freq_hz > 0:
            f_hz = summary.oscillation_freq_hz
            freq_str = (
                f"{f_hz / 1e3:.2f} kHz" if f_hz >= 1e3 else f"{f_hz:.1f} Hz"
            )
            lines.append(f"  Osc. freq: {freq_str}")
        if summary.charge_e is not None:
            lines.append(f"  Charge: {summary.charge_e:.0f} e")
        if summary.mz_Da_per_e is not None:
            lines.append(f"  m/z: {summary.mz_Da_per_e:.1f} Da/e")
        if summary.mass_Da is not None:
            mass = summary.mass_Da
            if mass >= 1e6:
                mass_str = f"{mass / 1e6:.3f} MDa"
            elif mass >= 1e3:
                mass_str = f"{mass / 1e3:.2f} kDa"
            else:
                mass_str = f"{mass:.1f} Da"
            lines.append(f"  Mass: {mass_str}")
        self.text_summary.setPlainText("\n".join(lines))

    def _set_controls_idle(self) -> None:
        connected = self._service.is_connected
        self.btn_connect.setEnabled(not connected)
        self.btn_disconnect.setEnabled(connected)
        self.btn_start.setEnabled(connected)
        self.btn_stop.setEnabled(False)
        self.combo_channel.setEnabled(True)
        self.combo_range.setEnabled(True)
        self.combo_coupling.setEnabled(True)
        self.spin_interval.setEnabled(True)
        self.spin_samples.setEnabled(True)
        self.chk_invert.setEnabled(True)
        self.spin_charge_cal.setEnabled(True)
        self.edit_trap_k.setEnabled(True)
        self.combo_stft_window.setEnabled(True)

    def _set_controls_running(self) -> None:
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.combo_channel.setEnabled(False)
        self.combo_range.setEnabled(False)
        self.combo_coupling.setEnabled(False)
        self.spin_interval.setEnabled(False)
        self.spin_samples.setEnabled(False)
        self.chk_invert.setEnabled(False)
        self.spin_charge_cal.setEnabled(False)
        self.edit_trap_k.setEnabled(False)
        self.combo_stft_window.setEnabled(False)

    @staticmethod
    def _set_label_good(label: QLabel, text: str) -> None:
        label.setText(text)
        label.setStyleSheet(f"color: {style.GOOD}; font-weight: bold;")

    @staticmethod
    def _set_label_bad(label: QLabel, text: str) -> None:
        label.setText(text)
        label.setStyleSheet(f"color: {style.BAD}; font-weight: bold;")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.stop_acquisition()
        if self._service.is_connected:
            self._service.disconnect()
        super().closeEvent(event)
