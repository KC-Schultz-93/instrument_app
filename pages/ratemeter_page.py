"""
ratemeter_page.py
------------------
Ratemeter GUI page: live diagnostic view for tuning ion optic voltages.

Shows how frequently particle signals occur within user-defined amplitude
bands (e.g. monomers vs. dimers vs. trimers), plus a live waveform strip for
visual confirmation. No data logging, no CDMS physics, stateless between runs.

Layout
------
PicoScope-style tiles + one docked option panel (see docs/ratemeter_ui.md):
  Top-left    status block: Connect/Run switches + lbl_status, lbl_trace_count
  Top bar     Scope / Trigger / Captures tiles
  Left rail   Channel / Detection / Rates tiles, Data Recorder pinned below
  Dock        ~320 px, one panel at a time, opened by clicking a tile
  Plots       waveform + rate trend (vertical splitter)
  Bottom      editable band table (left), live band readouts (right)

Threading model
----------------
Acquisition runs in RatemeterWorker (QThread). Acquisition controls auto-apply
by restarting the worker (debounced 300 ms for spin/combo edits, immediate for
band table edits) while a run is active.

Only one page may hold the PicoScope handle at a time — RatemeterPage owns its
own PicoScopeService instance and coordinates with DAQPage via the shared
DAQChannels.daq_busy signal.
"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

import pyqtgraph as pg
from PyQt5.QtCore import Qt, QSettings, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from instrument_app.services.daq_channels import DAQChannels
from instrument_app.services.daq_models import (
    AmplitudeBand,
    MatchedFilterConfig,
    PeakRecord,
    RatemeterConfig,
    RatemeterEvent,
    TimedRecordingSummary,
)
from instrument_app.services.picoscope_service import PicoScopeService
from instrument_app.services.probe_config import (
    NATIVE_VOLTAGE_RANGES_V,
    PROBE_FACTORS,
    format_voltage_label,
)
from instrument_app.services.ratemeter_logger import RatemeterLogger
from instrument_app.services.ratemeter_worker import RatemeterWorker
from instrument_app.services.timed_recording_logger import TimedRecordingLogger
from instrument_app.theme.style import style
from instrument_app.ui import CardFrame, DockHost, ParamTile, ToggleSwitch


# Same org/app identity as app/main.py's QSettings(APP_ORG, APP_NAME).
_APP_ORG = "JohnsonLab"
_APP_NAME = "NanoInstrumentApp"


_SAMPLE_INTERVALS = {
    "10 ns":  10,
    "20 ns":  20,
    "40 ns":  40,
    "80 ns":  80,
    "200 ns": 200,
    "1 µs":   1000,
}

_TRIGGER_DIRECTIONS = {
    "Rising": "RISING",
    "Falling": "FALLING",
    "Rising or Falling": "RISING_OR_FALLING",
}

_BAND_COLORS = [
    "#4fc3f7",  # light blue
    "#ff8a65",  # orange
    "#81c784",  # green
    "#ce93d8",  # purple
    "#fff176",  # yellow
    "#f48fb1",  # pink
    "#80cbc4",  # teal
    "#ffcc80",  # amber
]

_RAIL_WIDTH = 270
_BOTTOM_STRIP_HEIGHT = 200

_PLOT_MIN_INTERVAL_S = 0.1  # 10 Hz waveform refresh cap
_RESTART_DEBOUNCE_MS = 300

_WIDTH_REL_HEIGHT_MAP = {0: 0.5, 1: 0.2, 2: 0.1}

_MF_POLARITY_MAP = {
    "Positive first": "positive_first",
    "Negative first": "negative_first",
    "Both":           "both",
}
_MF_MAX_SCALES = 8
# pages/ -> package root -> templates/template.npy
_DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "template.npy"
_DETECTION_MODE_SIMPLE = "Simple threshold"
_DETECTION_MODE_MATCHED_FILTER = "Matched filter (bipolar)"


class TimedRecordingMetadataDialog(QDialog):
    """Prompts for run metadata after a timed-recording window completes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Timed Recording - Run Metadata")
        self.setModal(True)

        v = QVBoxLayout(self)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Description:"))
        self.edit_description = QLineEdit()
        row1.addWidget(self.edit_description, 1)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Vpp (V):"))
        self.spin_vpp = QDoubleSpinBox()
        self.spin_vpp.setRange(0.0, 10000.0)
        self.spin_vpp.setDecimals(4)
        row2.addWidget(self.spin_vpp, 1)
        v.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Frequency (Hz):"))
        self.spin_frequency = QDoubleSpinBox()
        self.spin_frequency.setRange(0.0, 1e9)
        self.spin_frequency.setDecimals(4)
        row3.addWidget(self.spin_frequency, 1)
        v.addLayout(row3)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_save = QPushButton("Save")
        self.btn_cancel = QPushButton("Cancel")
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_cancel)
        v.addLayout(btn_row)

        self.btn_save.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

    def description(self) -> str:
        return self.edit_description.text().strip()

    def vpp(self) -> float:
        return self.spin_vpp.value()

    def frequency_hz(self) -> float:
        return self.spin_frequency.value()


class RatemeterPage(QWidget):
    """Live band-rate diagnostic page."""

    def __init__(self, channels: DAQChannels, parent=None) -> None:
        super().__init__(parent)

        self.channels = channels
        self._service = PicoScopeService()
        self._worker: Optional[RatemeterWorker] = None
        self._daq_busy = False

        self._last_plot_update: float = 0.0
        self._band_lines: List[pg.InfiniteLine] = []
        self._rate_value_labels: Dict[str, QLabel] = {}
        self._transit_pct_labels: Dict[str, QLabel] = {}
        self._trend_curves: Dict[str, pg.PlotDataItem] = {}
        self._trend_times: Dict[str, deque] = {}
        self._trend_rates: Dict[str, deque] = {}
        self._legend = None

        self._logger: Optional[RatemeterLogger] = None
        self._recording: bool = False

        self._timed_recording_running: bool = False
        self._timed_recording_id: Optional[str] = None
        self._timed_recording_started_at: Optional[datetime] = None
        self._timed_peak_buffer: List[PeakRecord] = []

        self._settings = QSettings(_APP_ORG, _APP_NAME)
        # True while _load_settings() runs: widget setters fire signals that
        # call _save_settings(), which would overwrite not-yet-loaded keys
        # with widget defaults.
        self._loading_settings = False

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._restart_worker)

        self._timed_recording_timer = QTimer(self)
        self._timed_recording_timer.setSingleShot(True)
        self._timed_recording_timer.timeout.connect(self._on_timed_recording_elapsed)

        self._build_ui()
        self._load_settings()
        self._set_controls_idle()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Build order matters: all panels (create the control widgets) -> tiles
        # -> plots -> _load_settings(). Panel builders must not call
        # _schedule_restart because the plots/band table don't exist yet.
        self._tiles_ready = False

        self.dock = DockHost()
        panels = [
            ("channel", "Channel", self._make_channel_panel()),
            ("scope", "Scope", self._make_scope_panel()),
            ("trigger", "Trigger", self._make_trigger_group()),
            ("captures", "Captures", self._make_captures_panel()),
            ("detection", "Detection", self._make_detection_mode_group()),
            ("rates", "Rates", self._make_rates_panel()),
        ]
        for key, title, panel in panels:
            panel.layout().addStretch()
            self.dock.add_panel(key, title, panel)

        self._tiles: Dict[str, ParamTile] = {
            "channel": ParamTile("Channel", stepper=True),
            "scope": ParamTile("Scope", stepper=True),
            "trigger": ParamTile("Trigger", stepper=True),
            "captures": ParamTile("Captures", stepper=True),
            "detection": ParamTile("Detection"),
            "rates": ParamTile("Rates"),
        }
        for key, tile in self._tiles.items():
            tile.clicked.connect(lambda k=key: self.dock.toggle(k))
        self.dock.changed.connect(self._on_dock_changed)
        self._tiles["channel"].step_down.connect(lambda: self._step_range(-1))
        self._tiles["channel"].step_up.connect(lambda: self._step_range(+1))
        self._tiles["scope"].step_down.connect(lambda: self._step_window(-1))
        self._tiles["scope"].step_up.connect(lambda: self._step_window(+1))
        self._tiles["trigger"].step_down.connect(lambda: self._step_trigger(-1))
        self._tiles["trigger"].step_up.connect(lambda: self._step_trigger(+1))
        self._tiles["captures"].step_down.connect(lambda: self.spin_captures_per_batch.stepBy(-1))
        self._tiles["captures"].step_up.connect(lambda: self.spin_captures_per_batch.stepBy(+1))

        grid = QGridLayout(self)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setSpacing(6)
        grid.setColumnMinimumWidth(0, _RAIL_WIDTH)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)

        grid.addWidget(self._make_status_block(), 0, 0)
        grid.addWidget(self._make_top_bar(), 0, 1)
        grid.addWidget(self._make_left_rail(), 1, 0)

        body = QHBoxLayout()
        body.setSpacing(6)
        body.addWidget(self.dock)
        body.addWidget(self._make_right_panel(), 1)
        grid.addLayout(body, 1, 1)

        self._tiles_ready = True
        self.spin_trend_window.valueChanged.connect(self._refresh_tiles)
        self._refresh_tiles()

    # -- layout pieces ---------------------------------------------------

    @staticmethod
    def _make_panel():
        """Plain themed-by-parent panel to hold one dock page's controls."""
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)
        return panel, lay

    def _make_status_block(self) -> QWidget:
        """Connect/Run switches (left) beside the status readout (right)."""
        card = CardFrame()
        card.setFixedWidth(_RAIL_WIDTH)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(8)

        self.sw_connect = ToggleSwitch()
        self.sw_connect.clicked.connect(self._on_connect_switch)
        self.sw_run = ToggleSwitch()
        self.sw_run.clicked.connect(self._on_run_switch)

        switches = QGridLayout()
        switches.setHorizontalSpacing(6)
        switches.setVerticalSpacing(6)
        for row, (text, sw) in enumerate((("Connect", self.sw_connect), ("Run", self.sw_run))):
            lbl = QLabel(text)
            lbl.setStyleSheet("font: bold 10pt 'Segoe UI';")
            switches.addWidget(lbl, row, 0, Qt.AlignRight | Qt.AlignVCenter)
            switches.addWidget(sw, row, 1)
        lay.addLayout(switches)

        status = QVBoxLayout()
        status.setSpacing(2)
        self.lbl_status = QLabel("Idle")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setWordWrap(True)
        self.lbl_trace_count = QLabel("Traces:  0")
        self.lbl_trace_count.setAlignment(Qt.AlignCenter)
        self.lbl_connection = QLabel("Disconnected")
        self.lbl_connection.setAlignment(Qt.AlignCenter)
        self._set_label_bad(self.lbl_connection, "Disconnected")
        status.addWidget(self.lbl_status)
        status.addWidget(self.lbl_trace_count)
        status.addWidget(self.lbl_connection)
        lay.addLayout(status, 1)
        self._set_status("Idle")
        return card

    def _make_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("RmTopBar")
        bar.setStyleSheet("#RmTopBar{background:transparent;}")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for key in ("scope", "trigger", "captures"):
            lay.addWidget(self._tiles[key])
        lay.addStretch(1)
        return bar

    def _make_left_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("RmRail")
        rail.setStyleSheet("#RmRail{background:transparent;}")
        rail.setFixedWidth(_RAIL_WIDTH)
        lay = QVBoxLayout(rail)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for key in ("channel", "detection", "rates"):
            lay.addWidget(self._tiles[key])
        lay.addStretch(1)
        lay.addWidget(self._make_recorder_group())
        return rail

    def _on_dock_changed(self, key) -> None:
        for k, tile in self._tiles.items():
            tile.set_open(k == key)

    def _make_channel_panel(self) -> QWidget:
        box, lay = self._make_panel()

        lay.addWidget(QLabel("Channel:"))
        self.combo_channel = QComboBox()
        self.combo_channel.addItems(["A", "B"])
        self.combo_channel.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_channel)

        lay.addWidget(QLabel("Probe:"))
        self.combo_probe = QComboBox()
        self.combo_probe.addItems(list(PROBE_FACTORS.keys()))
        self.combo_probe.setCurrentText("x1")
        self.combo_probe.currentIndexChanged.connect(self._on_probe_changed)
        lay.addWidget(self.combo_probe)

        lay.addWidget(QLabel("Voltage range:"))
        self.combo_range = QComboBox()
        self._populate_range_combo()
        self.combo_range.setCurrentText("±20 mV")
        self.combo_range.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_range)

        lay.addWidget(QLabel("Coupling:"))
        self.combo_coupling = QComboBox()
        self.combo_coupling.addItems(["DC", "AC"])
        self.combo_coupling.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_coupling)

        self.chk_bandwidth_limit = QCheckBox("200 kHz bandwidth limit")
        self.chk_bandwidth_limit.stateChanged.connect(self._schedule_restart)
        lay.addWidget(self.chk_bandwidth_limit)

        return box

    def _make_scope_panel(self) -> QWidget:
        box, lay = self._make_panel()

        lay.addWidget(QLabel("Window duration (ms):"))
        self.spin_window = QDoubleSpinBox()
        self.spin_window.setRange(0.1, 1000.0)
        self.spin_window.setDecimals(2)
        self.spin_window.setValue(5.0)
        self.spin_window.valueChanged.connect(self._schedule_restart)
        self.spin_window.valueChanged.connect(self._update_mf_window_warning)
        lay.addWidget(self.spin_window)

        lay.addWidget(QLabel("Sample interval:"))
        self.combo_interval = QComboBox()
        for label in _SAMPLE_INTERVALS:
            self.combo_interval.addItem(label)
        self.combo_interval.setCurrentText("200 ns")
        self.combo_interval.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_interval)

        return box

    def _make_captures_panel(self) -> QWidget:
        box, lay = self._make_panel()

        lay.addWidget(QLabel("Captures per batch:"))
        self.spin_captures_per_batch = QSpinBox()
        self.spin_captures_per_batch.setRange(1, 1000)
        self.spin_captures_per_batch.setValue(1)
        self.spin_captures_per_batch.valueChanged.connect(self._schedule_restart)
        lay.addWidget(self.spin_captures_per_batch)

        return box

    def _make_trigger_group(self) -> QWidget:
        box, lay = self._make_panel()

        self.chk_trigger_enable = QCheckBox("Enable")
        self.chk_trigger_enable.stateChanged.connect(self._on_trigger_enabled_changed)
        lay.addWidget(self.chk_trigger_enable)

        lay.addWidget(QLabel("Threshold (mV):"))
        self.spin_trigger_threshold = QDoubleSpinBox()
        self.spin_trigger_threshold.setRange(-10000.0, 10000.0)
        self.spin_trigger_threshold.setValue(6.0)
        self.spin_trigger_threshold.valueChanged.connect(self._schedule_restart)
        lay.addWidget(self.spin_trigger_threshold)

        lay.addWidget(QLabel("Direction:"))
        self.combo_trigger_direction = QComboBox()
        self.combo_trigger_direction.addItems(list(_TRIGGER_DIRECTIONS.keys()))
        self.combo_trigger_direction.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_trigger_direction)

        lay.addWidget(QLabel("Auto-timeout (ms):"))
        self.spin_trigger_auto = QSpinBox()
        self.spin_trigger_auto.setRange(0, 10000)
        self.spin_trigger_auto.setValue(1000)
        self.spin_trigger_auto.valueChanged.connect(self._schedule_restart)
        lay.addWidget(self.spin_trigger_auto)

        # Initial enabled state — do not route through _schedule_restart here,
        # since the waveform plot and band table don't exist yet during
        # left-panel construction. _load_settings() calls the full handler
        # once the whole UI is built.
        enabled = self.chk_trigger_enable.isChecked()
        self.spin_trigger_threshold.setEnabled(enabled)
        self.combo_trigger_direction.setEnabled(enabled)
        self.spin_trigger_auto.setEnabled(enabled)
        return box

    def _make_detection_mode_group(self) -> QWidget:
        box, lay = self._make_panel()

        lay.addWidget(QLabel("Mode:"))
        self.combo_detection_mode = QComboBox()
        self.combo_detection_mode.addItems([_DETECTION_MODE_SIMPLE, _DETECTION_MODE_MATCHED_FILTER])
        self.combo_detection_mode.currentIndexChanged.connect(self._on_detection_mode_changed)
        lay.addWidget(self.combo_detection_mode)

        self._mf_controls = QWidget()
        self._mf_controls.setObjectName("MfControls")
        self._mf_controls.setStyleSheet("#MfControls{background:transparent;}")
        mf_lay = QVBoxLayout(self._mf_controls)
        mf_lay.setContentsMargins(0, 0, 0, 0)

        mf_lay.addWidget(QLabel("Pulse duration, peak-to-peak (µs):"))
        self.spin_mf_pulse = QDoubleSpinBox()
        self.spin_mf_pulse.setRange(10.0, 500.0)
        self.spin_mf_pulse.setValue(85.0)
        self.spin_mf_pulse.valueChanged.connect(self._schedule_restart)
        self.spin_mf_pulse.valueChanged.connect(self._update_mf_window_warning)
        self.spin_mf_pulse.valueChanged.connect(self._update_mf_scale_preview)
        mf_lay.addWidget(self.spin_mf_pulse)

        mf_lay.addWidget(QLabel("Pulse min (µs):"))
        self.spin_mf_pulse_min = QDoubleSpinBox()
        self.spin_mf_pulse_min.setRange(10.0, 500.0)
        self.spin_mf_pulse_min.setValue(60.0)
        self.spin_mf_pulse_min.valueChanged.connect(self._schedule_restart)
        mf_lay.addWidget(self.spin_mf_pulse_min)

        mf_lay.addWidget(QLabel("Pulse max (µs):"))
        self.spin_mf_pulse_max = QDoubleSpinBox()
        self.spin_mf_pulse_max.setRange(10.0, 500.0)
        self.spin_mf_pulse_max.setValue(110.0)
        self.spin_mf_pulse_max.valueChanged.connect(self._schedule_restart)
        self.spin_mf_pulse_max.valueChanged.connect(self._update_mf_window_warning)
        mf_lay.addWidget(self.spin_mf_pulse_max)

        mf_lay.addWidget(QLabel("Scale factors (comma-separated):"))
        self.edit_mf_scales = QLineEdit("1.0")
        self.edit_mf_scales.setToolTip(
            "Template stretch factors tried on every trace, e.g. 0.75, 1.0, 1.5, 2.0.\n"
            "Pulse min/max are multiplied by each scale. Up to 8 values."
        )
        self.edit_mf_scales.textChanged.connect(self._on_mf_scales_changed)
        mf_lay.addWidget(self.edit_mf_scales)

        self.lbl_mf_scale_preview = QLabel("")
        self.lbl_mf_scale_preview.setWordWrap(True)
        self.lbl_mf_scale_preview.setStyleSheet(f"color: {style.TXT_MUTED}; font-size: 9pt;")
        mf_lay.addWidget(self.lbl_mf_scale_preview)

        mf_lay.addWidget(QLabel("Polarity:"))
        self.combo_mf_polarity = QComboBox()
        self.combo_mf_polarity.addItems(list(_MF_POLARITY_MAP.keys()))
        self.combo_mf_polarity.currentIndexChanged.connect(self._schedule_restart)
        mf_lay.addWidget(self.combo_mf_polarity)

        mf_lay.addWidget(QLabel("Correlation threshold:"))
        self.spin_mf_threshold = QDoubleSpinBox()
        self.spin_mf_threshold.setRange(0.05, 0.99)
        self.spin_mf_threshold.setSingleStep(0.01)
        self.spin_mf_threshold.setDecimals(2)
        self.spin_mf_threshold.setValue(0.35)
        self.spin_mf_threshold.valueChanged.connect(self._schedule_restart)
        mf_lay.addWidget(self.spin_mf_threshold)

        mf_lay.addWidget(QLabel("Min event spacing (µs):"))
        self.spin_mf_min_spacing = QDoubleSpinBox()
        self.spin_mf_min_spacing.setRange(10.0, 1000.0)
        self.spin_mf_min_spacing.setValue(50.0)
        self.spin_mf_min_spacing.valueChanged.connect(self._schedule_restart)
        mf_lay.addWidget(self.spin_mf_min_spacing)

        self.chk_mf_use_empirical = QCheckBox("Use empirical template (.npy)")
        self.chk_mf_use_empirical.setToolTip(
            "The template's own peak-to-peak is taken to equal the pulse duration above,\n"
            "so it is resampled to the scope's sample interval automatically."
        )
        self.chk_mf_use_empirical.stateChanged.connect(self._on_mf_use_empirical_changed)
        mf_lay.addWidget(self.chk_mf_use_empirical)

        self.btn_mf_load_template = QPushButton("Load empirical template (.npy)")
        self.btn_mf_load_template.setEnabled(False)
        self.btn_mf_load_template.clicked.connect(self._on_mf_load_template_clicked)
        mf_lay.addWidget(self.btn_mf_load_template)

        self.lbl_mf_template_path = QLabel("(none)")
        self.lbl_mf_template_path.setWordWrap(True)
        self.lbl_mf_template_path.setStyleSheet(f"color: {style.TXT_MUTED}; font-size: 9pt;")
        mf_lay.addWidget(self.lbl_mf_template_path)
        self._mf_empirical_template_path = ""
        self._set_mf_template_path(str(_DEFAULT_TEMPLATE_PATH) if _DEFAULT_TEMPLATE_PATH.exists() else "")

        self.lbl_mf_window_warning = QLabel("")
        self.lbl_mf_window_warning.setWordWrap(True)
        self.lbl_mf_window_warning.setStyleSheet(f"color: {style.BAD}; font-size: 9pt; font-weight: bold;")
        mf_lay.addWidget(self.lbl_mf_window_warning)

        note = QLabel(
            "Matched filter detects sub-threshold bipolar pulses by shape,\n"
            "not amplitude. Overrides the simple-threshold peak finder."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {style.TXT_MUTED}; font-size: 9pt;")
        mf_lay.addWidget(note)

        lay.addWidget(self._mf_controls)
        self._mf_controls.setVisible(False)
        return box

    def _make_rates_panel(self) -> QWidget:
        """Rate averaging, trend window and transit-width settings."""
        box, lay = self._make_panel()

        lay.addWidget(QLabel("Rate averaging window (s):"))
        self.spin_rate_avg = QSpinBox()
        self.spin_rate_avg.setRange(1, 120)
        self.spin_rate_avg.setValue(10)
        self.spin_rate_avg.valueChanged.connect(self._schedule_restart)
        lay.addWidget(self.spin_rate_avg)

        lay.addWidget(QLabel("Trend plot window (s):"))
        self.spin_trend_window = QSpinBox()
        self.spin_trend_window.setRange(10, 300)
        self.spin_trend_window.setValue(60)
        self.spin_trend_window.valueChanged.connect(self._save_settings)
        lay.addWidget(self.spin_trend_window)

        lay.addWidget(QLabel("Electrode length:  1.3 in  (33.0 mm)  [fixed]"))

        lay.addWidget(QLabel("Measure width at:"))
        self.combo_width_rel_height = QComboBox()
        self.combo_width_rel_height.addItems([
            "50%  (FWHM — default)",
            "20%  (near base)",
            "10%  (base width)",
        ])
        self.combo_width_rel_height.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_width_rel_height)

        note = QLabel(
            "Set a minimum width in the Bands table to enable\n"
            "transit % and velocity display for that band.\n"
            "Signals below the threshold are counted as splat.\n"
            "Ignored in Matched Filter mode — all matched-filter\n"
            "events are classified as \"unknown\"."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {style.TXT_MUTED}; font-size: 9pt;")
        lay.addWidget(note)

        return box

    def _make_bands_group(self) -> QWidget:
        box = QWidget()
        box.setObjectName("RmBands")
        box.setStyleSheet("#RmBands{background:transparent;}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)

        self.table_bands = QTableWidget(0, 5)
        self.table_bands.setHorizontalHeaderLabels(
            ["#", "Low (mV)", "High (mV)", "Color", "Min width (ns)"]
        )
        header = self.table_bands.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for col in (1, 2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.Stretch)
        self.table_bands.setMinimumHeight(110)
        self.table_bands.itemChanged.connect(self._on_band_item_changed)
        self.table_bands.cellDoubleClicked.connect(self._on_band_cell_double_clicked)
        lay.addWidget(self.table_bands)

        btn_row = QHBoxLayout()
        self.btn_add_band = QPushButton("+ Add")
        self.btn_add_band.clicked.connect(self._add_band_row)
        self.btn_remove_band = QPushButton("- Remove")
        self.btn_remove_band.clicked.connect(self._remove_band_row)
        btn_row.addWidget(self.btn_add_band)
        btn_row.addWidget(self.btn_remove_band)
        lay.addLayout(btn_row)

        return box

    def _make_recorder_group(self) -> QGroupBox:
        box = QGroupBox("Data Recorder")
        lay = QVBoxLayout(box)

        self.btn_record = QPushButton("⏺  Record Data")
        self.btn_record.setCheckable(True)
        self.btn_record.setEnabled(False)   # enabled only while acquisition is running
        self.btn_record.setToolTip(
            "Write all detected peak events to a CSV file.\n"
            "Includes timestamp, band, amplitude, width, event type, and velocity."
        )
        self.btn_record.clicked.connect(self._on_record_toggled)
        lay.addWidget(self.btn_record)

        self.lbl_recording = QLabel("")
        self.lbl_recording.setStyleSheet("color: #ef5350; font: bold 9pt;")
        self.lbl_recording.setWordWrap(True)
        lay.addWidget(self.lbl_recording)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        lay.addWidget(sep2)

        dur_row = QHBoxLayout()
        dur_row.addWidget(QLabel("Timed recording duration (s):"))
        self.spin_timed_duration = QSpinBox()
        self.spin_timed_duration.setRange(1, 3600)
        self.spin_timed_duration.setValue(30)
        dur_row.addWidget(self.spin_timed_duration)
        lay.addLayout(dur_row)

        self.btn_timed_record = QPushButton("⏱  Timed Recording")
        self.btn_timed_record.setEnabled(False)   # enabled only while acquisition is running
        self.btn_timed_record.setToolTip(
            "Capture every detected peak's raw amplitude for a fixed duration,\n"
            "then prompt for run metadata (description, Vpp, frequency) before saving.\n"
            "Live waveform plotting pauses during capture."
        )
        self.btn_timed_record.clicked.connect(self._on_timed_record_clicked)
        lay.addWidget(self.btn_timed_record)

        self.lbl_timed_recording = QLabel("")
        self.lbl_timed_recording.setStyleSheet("color: #29b6f6; font: bold 9pt;")
        self.lbl_timed_recording.setWordWrap(True)
        lay.addWidget(self.lbl_timed_recording)

        return box

    def _make_right_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("RmRight")
        panel.setStyleSheet("#RmRight{background:transparent;}")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(self._make_waveform_plot())
        vsplit.addWidget(self._make_trend_plot())
        vsplit.setSizes([320, 250])
        lay.addWidget(vsplit, 1)
        lay.addWidget(self._make_bottom_strip())
        return panel

    def _make_bottom_strip(self) -> QWidget:
        """Band table (left) and live band readouts (right)."""
        strip = QWidget()
        strip.setObjectName("RmStrip")
        strip.setStyleSheet("#RmStrip{background:transparent;}")
        strip.setFixedHeight(_BOTTOM_STRIP_HEIGHT)
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        bands_card = CardFrame()
        bands_lay = QVBoxLayout(bands_card)
        bands_lay.setContentsMargins(6, 6, 6, 6)
        bands_lay.addWidget(self._make_bands_group())
        lay.addWidget(bands_card, 1)

        rates_card = CardFrame()
        rates_lay = QVBoxLayout(rates_card)
        rates_lay.setContentsMargins(2, 2, 2, 2)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;}")
        scroll.setWidget(self._make_rates_frame())
        rates_lay.addWidget(scroll)
        lay.addWidget(rates_card, 1)
        return strip

    def _make_waveform_plot(self) -> QWidget:
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground(style.PLOT_BG)
        self.plot_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        pi = self.plot_widget.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.3)
        pi.setLabel("bottom", "Time", units="µs")
        pi.setLabel("left", "Voltage", units="mV")
        pi.setTitle("Waveform")

        axis_pen = pg.mkPen(color=style.BTN_BORDER)
        for ax in ("bottom", "left"):
            pi.getAxis(ax).setPen(axis_pen)
            pi.getAxis(ax).setTextPen(style.TXT)

        self._plot_item = self.plot_widget.plot([], [], pen=pg.mkPen(color=style.GOOD, width=1))

        self._trigger_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(color="w", width=1, style=Qt.DashLine)
        )
        self._trigger_line.setVisible(False)
        self.plot_widget.addItem(self._trigger_line)

        # Matched-filter correlation overlay — second y-axis on the right,
        # hidden unless matched filter mode is active.
        pi.showAxis("right")
        pi.getAxis("right").setLabel("Correlation score", units="")
        pi.getAxis("right").setPen(axis_pen)
        pi.getAxis("right").setTextPen(style.TXT)
        pi.getAxis("right").setVisible(False)

        self._corr_viewbox = pg.ViewBox()
        pi.scene().addItem(self._corr_viewbox)
        pi.getAxis("right").linkToView(self._corr_viewbox)
        self._corr_viewbox.setXLink(pi)
        self._corr_viewbox.setYRange(-1.0, 1.0, padding=0)
        pi.vb.sigResized.connect(self._update_corr_viewbox_geometry)

        self._corr_curve = pg.PlotDataItem(pen=pg.mkPen(style.TXT, width=1, style=Qt.DotLine))
        self._corr_curve.setVisible(False)
        self._corr_viewbox.addItem(self._corr_curve)
        self._last_time_us = None
        self._last_corr_plot_update = 0.0

        return self.plot_widget

    def _update_corr_viewbox_geometry(self) -> None:
        self._corr_viewbox.setGeometry(self.plot_widget.getPlotItem().vb.sceneBoundingRect())

    def _update_corr_overlay_visibility(self, enabled: bool) -> None:
        self.plot_widget.getPlotItem().getAxis("right").setVisible(enabled)
        self._corr_curve.setVisible(enabled)
        if not enabled:
            self._corr_curve.setData([], [])

    def _make_rates_frame(self) -> QWidget:
        self.rates_frame = QFrame()
        self.rates_frame.setObjectName("RmRates")
        self.rates_frame.setStyleSheet("#RmRates{background:transparent;}")
        self.rates_layout = QVBoxLayout(self.rates_frame)
        self.rates_layout.setContentsMargins(6, 6, 6, 6)
        self.rates_layout.setSpacing(4)
        return self.rates_frame

    def _make_trend_plot(self) -> QWidget:
        self.trend_widget = pg.PlotWidget()
        self.trend_widget.setBackground(style.PLOT_BG)
        self.trend_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        ti = self.trend_widget.getPlotItem()
        ti.showGrid(x=True, y=True, alpha=0.3)
        ti.setLabel("bottom", "Time", units="s")
        ti.setLabel("left", "Rate", units="Hz")
        ti.setTitle("Rate Trend")

        axis_pen = pg.mkPen(color=style.BTN_BORDER)
        for ax in ("bottom", "left"):
            ti.getAxis(ax).setPen(axis_pen)
            ti.getAxis(ax).setTextPen(style.TXT)

        self._legend = self.trend_widget.addLegend()
        return self.trend_widget

    # ------------------------------------------------------------------
    # Band table
    # ------------------------------------------------------------------

    def _add_band_row(self) -> None:
        row = self.table_bands.rowCount()
        self.table_bands.blockSignals(True)
        self.table_bands.insertRow(row)
        self._write_band_row(row, 0.0, 0.0, _BAND_COLORS[row % len(_BAND_COLORS)])
        self.table_bands.blockSignals(False)
        self._on_bands_changed()

    def _remove_band_row(self) -> None:
        rows = sorted({idx.row() for idx in self.table_bands.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self.table_bands.blockSignals(True)
        for row in rows:
            self.table_bands.removeRow(row)
        self._renumber_rows()
        self.table_bands.blockSignals(False)
        self._on_bands_changed()

    def _renumber_rows(self) -> None:
        for row in range(self.table_bands.rowCount()):
            item = self.table_bands.item(row, 0)
            if item is not None:
                item.setText(str(row + 1))

    def _write_band_row(
        self,
        row: int,
        low_mv: float,
        high_mv: float,
        color: str,
        transit_min_width_ns: Optional[float] = None,
    ) -> None:
        num_item = QTableWidgetItem(str(row + 1))
        num_item.setFlags(num_item.flags() & ~Qt.ItemIsEditable)
        self.table_bands.setItem(row, 0, num_item)
        self.table_bands.setItem(row, 1, QTableWidgetItem(f"{low_mv:g}"))
        self.table_bands.setItem(row, 2, QTableWidgetItem(f"{high_mv:g}"))

        color_item = QTableWidgetItem("")
        color_item.setFlags((color_item.flags() & ~Qt.ItemIsEditable) | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        color_item.setData(Qt.UserRole, color)
        color_item.setBackground(QColor(color))
        self.table_bands.setItem(row, 3, color_item)

        width_text = f"{transit_min_width_ns:g}" if transit_min_width_ns is not None else ""
        self.table_bands.setItem(row, 4, QTableWidgetItem(width_text))

    def _on_band_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() in (1, 2, 4):
            self._on_bands_changed()

    def _on_band_cell_double_clicked(self, row: int, col: int) -> None:
        if col != 3:
            return
        item = self.table_bands.item(row, col)
        if item is None:
            return
        current = QColor(item.data(Qt.UserRole) or _BAND_COLORS[0])
        color = QColorDialog.getColor(current, self)
        if color.isValid():
            item.setData(Qt.UserRole, color.name())
            item.setBackground(color)
            self._on_bands_changed()

    def _bands_from_table(self) -> List[AmplitudeBand]:
        bands = []
        for row in range(self.table_bands.rowCount()):
            label = f"Band {row + 1}"
            try:
                low = float(self.table_bands.item(row, 1).text())
            except (AttributeError, ValueError):
                low = 0.0
            try:
                high = float(self.table_bands.item(row, 2).text())
            except (AttributeError, ValueError):
                high = 0.0
            color_item = self.table_bands.item(row, 3)
            color = (color_item.data(Qt.UserRole) if color_item else None) or _BAND_COLORS[0]
            try:
                w_text = self.table_bands.item(row, 4).text().strip()
                transit_min_width_ns = float(w_text) if w_text else None
            except (AttributeError, ValueError):
                transit_min_width_ns = None
            bands.append(AmplitudeBand(
                label=label, low_mv=low, high_mv=high, color=color,
                transit_min_width_ns=transit_min_width_ns,
            ))
        return bands

    def _on_bands_changed(self) -> None:
        self._rebuild_band_dependent_ui()
        self._save_settings()
        if self._worker is not None:
            self._restart_worker()

    # ------------------------------------------------------------------
    # Band-dependent UI (waveform lines, rate rows, trend curves)
    # ------------------------------------------------------------------

    def _rebuild_band_dependent_ui(self) -> None:
        bands = self._bands_from_table()
        self._rebuild_band_lines(bands)
        self._rebuild_rate_rows(bands)
        self._rebuild_trend_plot(bands)

    def _rebuild_band_lines(self, bands: List[AmplitudeBand]) -> None:
        for line in self._band_lines:
            self.plot_widget.removeItem(line)
        self._band_lines = []
        for band in bands:
            for mv in (band.low_mv, band.high_mv):
                line = pg.InfiniteLine(
                    pos=mv, angle=0,
                    pen=pg.mkPen(color=band.color, width=1, style=Qt.DashLine),
                )
                self.plot_widget.addItem(line)
                self._band_lines.append(line)

    def _rebuild_rate_rows(self, bands: List[AmplitudeBand]) -> None:
        self._clear_layout(self.rates_layout)
        self._rate_value_labels = {}
        self._transit_pct_labels = {}

        for band in bands:
            band_widget = QWidget()
            vlay = QVBoxLayout(band_widget)
            vlay.setSpacing(2)
            vlay.setContentsMargins(0, 4, 0, 4)

            top_row = QHBoxLayout()
            swatch = QLabel()
            swatch.setFixedSize(14, 14)
            swatch.setStyleSheet(f"background: {band.color}; border-radius: 3px;")

            text = QLabel(f"{band.label}   {band.low_mv:g} – {band.high_mv:g} mV")
            text.setStyleSheet(f"color: {style.TXT};")

            rate_label = QLabel("0.0 Hz")
            rate_label.setStyleSheet(f"color: {band.color}; font: bold 18pt 'Consolas';")

            top_row.addWidget(swatch)
            top_row.addWidget(text)
            top_row.addStretch()
            top_row.addWidget(rate_label)
            vlay.addLayout(top_row)

            # Second row — transit % and velocity (empty when threshold not set)
            pct_label = QLabel("")
            pct_label.setStyleSheet(
                f"color: {style.TXT_MUTED}; font: 10pt 'Consolas'; padding-left: 22px;"
            )
            vlay.addWidget(pct_label)

            self.rates_layout.addWidget(band_widget)
            self._rate_value_labels[band.label] = rate_label
            self._transit_pct_labels[band.label] = pct_label

    def _rebuild_trend_plot(self, bands: List[AmplitudeBand]) -> None:
        for curve in self._trend_curves.values():
            self.trend_widget.removeItem(curve)
        self._trend_curves = {}
        self._trend_times = {}
        self._trend_rates = {}
        if self._legend is not None:
            try:
                self._legend.clear()
            except Exception:
                pass
        for band in bands:
            curve = self.trend_widget.plot(
                [], [], pen=pg.mkPen(color=band.color, width=2), name=band.label
            )
            self._trend_curves[band.label] = curve
            self._trend_times[band.label] = deque()
            self._trend_rates[band.label] = deque()

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    # ------------------------------------------------------------------
    # Plot axis / trigger line helpers
    # ------------------------------------------------------------------

    def _update_plot_axes(self) -> None:
        voltage_range_v = self._current_true_voltage_range_v()
        range_mv = voltage_range_v * 1000
        self.plot_widget.setYRange(-range_mv, range_mv, padding=0)
        window_us = self.spin_window.value() * 1000
        self.plot_widget.setXRange(0, window_us, padding=0)

    def _update_trigger_line(self) -> None:
        enabled = self.chk_trigger_enable.isChecked()
        self._trigger_line.setVisible(enabled)
        if enabled:
            self._trigger_line.setPos(self.spin_trigger_threshold.value())

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_connect_clicked(self) -> None:
        try:
            self._service.connect()
        except Exception as exc:
            self._on_error(f"Connect failed: {exc}")
            self._set_label_bad(self.lbl_connection, "Connect failed")
            return
        self._set_label_good(self.lbl_connection, "Connected")
        self._set_controls_idle()

    def _on_disconnect_clicked(self) -> None:
        self.stop_acquisition()
        try:
            self._service.disconnect()
        except Exception as exc:
            self._on_error(f"Disconnect error: {exc}")
        self._set_label_bad(self.lbl_connection, "Disconnected")
        self._set_controls_idle()

    def _on_start_clicked(self) -> None:
        if self._daq_busy:
            QMessageBox.warning(self, "PicoScope Busy", "PicoScope is in use by the DAQ page.")
            return
        if self._worker is not None:
            return  # already running

        if not self._service.is_connected:
            try:
                self._service.connect()
            except Exception as exc:
                self._on_error(f"Connect failed: {exc}")
                return
            self._set_label_good(self.lbl_connection, "Connected")

        config = self._build_config()
        try:
            acq_config = config.to_acquisition_config(
                trigger_enabled=self.chk_trigger_enable.isChecked(),
                trigger_threshold_v=self.spin_trigger_threshold.value() / 1000.0,
                trigger_direction=self._trigger_direction_value(),
            )
            self._service.configure_channel(acq_config)
            self._service.set_trigger(acq_config, auto_trigger_ms=self.spin_trigger_auto.value())
        except Exception as exc:
            self._on_error(f"Hardware config error: {exc}")
            return

        self._start_worker(config, self._build_mf_config())
        self.channels.daq_busy.emit(True)
        self._set_controls_running()
        self._set_status("Running")

    def _on_stop_clicked(self) -> None:
        self.stop_acquisition()

    def stop_acquisition(self) -> None:
        """Stop the worker gracefully. Safe to call even if not running."""
        self._stop_recording()
        self.btn_record.setChecked(False)
        self._cancel_timed_recording()

        if self._worker is None:
            return

        self._worker.request_stop()
        finished = self._worker.wait(5000)
        if not finished:
            self._worker.terminate()
            self._worker.wait(1000)
        self._worker = None

        self.channels.daq_busy.emit(False)
        self._set_controls_idle()
        self._set_status("Stopped")

    def _start_worker(self, config: RatemeterConfig, mf_config: MatchedFilterConfig) -> None:
        trigger_enabled = self.chk_trigger_enable.isChecked()
        trigger_threshold_v = self.spin_trigger_threshold.value() / 1000.0
        trigger_direction = self._trigger_direction_value()

        self._worker = RatemeterWorker(
            self._service, config, mf_config, trigger_enabled, trigger_threshold_v, trigger_direction,
            captures_per_batch=self.spin_captures_per_batch.value(),
        )
        self._worker.rates_updated.connect(self._on_rates_updated)
        self._worker.waveform_ready.connect(self._on_waveform_ready)
        self._worker.correlation_ready.connect(self._on_correlation_ready)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.status_update.connect(self._on_status)
        self._worker.trace_count_changed.connect(self._on_trace_count)
        self._worker.peak_event.connect(self._on_peak_event)
        self._worker.start()

    def _on_peak_event(self, event: RatemeterEvent) -> None:
        """Route peak events to the logger when recording is active."""
        if self._recording and self._logger:
            self._logger.save_event(event)

    def _on_record_toggled(self, checked: bool) -> None:
        if checked:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        run_id = RatemeterLogger.make_run_id()
        self._logger = RatemeterLogger(RatemeterLogger.default_base_dir(), run_id)
        self._recording = True
        self.btn_record.setText("⏹  Stop Recording")
        self.lbl_recording.setText(f"● REC  {self._logger.path.name}")

    def _stop_recording(self) -> None:
        if self._logger:
            self._logger.close()
            self._logger = None
        self._recording = False
        self.btn_record.setText("⏺  Record Data")
        self.lbl_recording.setText("")

    def _on_timed_record_clicked(self) -> None:
        if self._worker is None or self._timed_recording_running:
            return

        self._timed_recording_running = True
        self._timed_recording_id = TimedRecordingLogger.make_run_id()
        self._timed_recording_started_at = datetime.now()
        self._timed_peak_buffer = []
        self._worker.raw_peaks_detected.connect(self._on_raw_peaks_for_timed_recording)

        self.btn_timed_record.setEnabled(False)
        self.spin_timed_duration.setEnabled(False)
        self.lbl_timed_recording.setText(
            f"⏱  Timed recording... ({self.spin_timed_duration.value()} s, waveform plot paused)"
        )

        self._timed_recording_timer.start(int(self.spin_timed_duration.value() * 1000))

    def _on_raw_peaks_for_timed_recording(self, peaks) -> None:
        self._timed_peak_buffer.extend(peaks)

    def _on_timed_recording_elapsed(self) -> None:
        if self._worker is not None:
            try:
                self._worker.raw_peaks_detected.disconnect(self._on_raw_peaks_for_timed_recording)
            except TypeError:
                pass  # already disconnected (e.g. worker was replaced mid-window)
        self._timed_recording_running = False  # resumes waveform plotting immediately

        dlg = TimedRecordingMetadataDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            summary = TimedRecordingSummary(
                recording_id=self._timed_recording_id,
                timestamp=self._timed_recording_started_at,
                description=dlg.description(),
                vpp=dlg.vpp(),
                frequency_hz=dlg.frequency_hz(),
                duration_s=float(self.spin_timed_duration.value()),
                total_peak_count=len(self._timed_peak_buffer),
                channel=self.combo_channel.currentText(),
                voltage_range_v=self._current_true_voltage_range_v(),
                coupling=self.combo_coupling.currentText(),
                sample_interval_ns=_SAMPLE_INTERVALS.get(self.combo_interval.currentText(), 200),
                window_duration_ms=self.spin_window.value(),
                trigger_enabled=self.chk_trigger_enable.isChecked(),
                trigger_threshold_mv=self.spin_trigger_threshold.value(),
            )
            logger = TimedRecordingLogger(TimedRecordingLogger.default_base_dir())
            try:
                logger.save_recording(summary)
                logger.save_peaks(self._timed_recording_id, self._timed_peak_buffer)
            except OSError as exc:
                self._on_error(f"Timed recording save failed: {exc}")

        self._reset_timed_recording_ui()

    def _reset_timed_recording_ui(self) -> None:
        self._timed_peak_buffer = []
        self._timed_recording_id = None
        self._timed_recording_started_at = None
        self.btn_timed_record.setEnabled(self._worker is not None)
        self.spin_timed_duration.setEnabled(True)
        self.lbl_timed_recording.setText("")

    def _cancel_timed_recording(self) -> None:
        """Abort an in-progress timed recording and discard captured peaks."""
        if not self._timed_recording_running:
            return
        self._timed_recording_timer.stop()
        if self._worker is not None:
            try:
                self._worker.raw_peaks_detected.disconnect(self._on_raw_peaks_for_timed_recording)
            except TypeError:
                pass
        self._timed_recording_running = False
        self._reset_timed_recording_ui()

    def _restart_worker(self) -> None:
        if self._worker is None:
            return  # not running — control changes take effect at next Start

        self._worker.request_stop()
        finished = self._worker.wait(3000)
        if not finished:
            self._worker.terminate()
            self._worker.wait(1000)
        self._worker = None

        config = self._build_config()
        try:
            acq_config = config.to_acquisition_config(
                trigger_enabled=self.chk_trigger_enable.isChecked(),
                trigger_threshold_v=self.spin_trigger_threshold.value() / 1000.0,
                trigger_direction=self._trigger_direction_value(),
            )
            self._service.configure_channel(acq_config)
            self._service.set_trigger(acq_config, auto_trigger_ms=self.spin_trigger_auto.value())
        except Exception as exc:
            self._on_error(f"Hardware config error: {exc}")
            self.channels.daq_busy.emit(False)
            self._set_controls_idle()
            return

        self._start_worker(config, self._build_mf_config())
        if self._timed_recording_running:
            self._worker.raw_peaks_detected.connect(self._on_raw_peaks_for_timed_recording)
        self._set_status("Running")

    def _schedule_restart(self, *_args) -> None:
        self._update_plot_axes()
        self._update_trigger_line()
        self._save_settings()
        self._refresh_tiles()
        if self._worker is not None:
            self._debounce_timer.start(_RESTART_DEBOUNCE_MS)

    def _on_trigger_enabled_changed(self, _state=None) -> None:
        enabled = self.chk_trigger_enable.isChecked()
        self.spin_trigger_threshold.setEnabled(enabled)
        self.combo_trigger_direction.setEnabled(enabled)
        self.spin_trigger_auto.setEnabled(enabled)
        self._schedule_restart()

    def _on_detection_mode_changed(self, _index=None) -> None:
        enabled = self.combo_detection_mode.currentText() == _DETECTION_MODE_MATCHED_FILTER
        self._mf_controls.setVisible(enabled)
        self._update_corr_overlay_visibility(enabled)
        self._update_mf_window_warning()
        self._schedule_restart()

    def _on_mf_use_empirical_changed(self, _state=None) -> None:
        self.btn_mf_load_template.setEnabled(self.chk_mf_use_empirical.isChecked())
        self._schedule_restart()

    def _set_mf_template_path(self, path: str) -> None:
        self._mf_empirical_template_path = path
        self.lbl_mf_template_path.setText(path or "(none)")

    def _on_mf_load_template_clicked(self) -> None:
        start_dir = str(_DEFAULT_TEMPLATE_PATH.parent) if _DEFAULT_TEMPLATE_PATH.parent.exists() else ""
        path, _ = QFileDialog.getOpenFileName(self, "Load Empirical Template", start_dir, "NumPy array (*.npy)")
        if path:
            self._set_mf_template_path(path)
            self._schedule_restart()

    def _parse_mf_scales(self) -> Optional[List[float]]:
        """Parse the scale-factor field. Returns None if it is invalid."""
        parts = [p.strip() for p in self.edit_mf_scales.text().split(",") if p.strip()]
        try:
            scales = [float(p) for p in parts]
        except ValueError:
            return None
        if not scales or any(s <= 0 for s in scales):
            return None
        return scales

    def _on_mf_scales_changed(self, _text=None) -> None:
        self._update_mf_scale_preview()
        self._update_mf_window_warning()
        if self._parse_mf_scales() is not None:
            self._schedule_restart()

    def _update_mf_scale_preview(self, *_args) -> None:
        scales = self._parse_mf_scales()
        if scales is None:
            self.lbl_mf_scale_preview.setText("⚠ Enter positive numbers separated by commas.")
            return
        durations = ", ".join(f"{self.spin_mf_pulse.value() * s:.0f}" for s in scales)
        text = f"→ {durations} µs"
        if len(scales) > _MF_MAX_SCALES:
            text += f"\n⚠ More than {_MF_MAX_SCALES} scales may cause acquisition lag"
        self.lbl_mf_scale_preview.setText(text)

    def _update_mf_window_warning(self, *_args) -> None:
        if self.combo_detection_mode.currentText() != _DETECTION_MODE_MATCHED_FILTER:
            self.lbl_mf_window_warning.setText("")
            self._refresh_tiles()
            return
        largest_scale = max(self._parse_mf_scales() or [1.0])
        min_window_ms = self.spin_mf_pulse_max.value() * largest_scale * 3 / 1000.0
        if self.spin_window.value() < min_window_ms:
            self.lbl_mf_window_warning.setText(
                f"⚠ Window duration ({self.spin_window.value():.2f} ms) is shorter than "
                f"3× largest pulse max ({min_window_ms:.2f} ms). Correlation may be unreliable at trace edges."
            )
        else:
            self.lbl_mf_window_warning.setText("")
        self._refresh_tiles()

    # ------------------------------------------------------------------
    # Worker signal slots (main thread)
    # ------------------------------------------------------------------

    def _on_waveform_ready(self, record) -> None:
        if self._timed_recording_running:
            return
        now = time.monotonic()
        if now - self._last_plot_update < _PLOT_MIN_INTERVAL_S:
            return
        self._last_plot_update = now

        time_us = record.time_ns / 1e3
        voltage_mv = record.voltage * 1000
        self._plot_item.setData(time_us, voltage_mv)
        self._last_time_us = time_us

    def _on_correlation_ready(self, corr) -> None:
        if self._timed_recording_running:
            return
        if self.combo_detection_mode.currentText() != _DETECTION_MODE_MATCHED_FILTER:
            return
        now = time.monotonic()
        if now - self._last_corr_plot_update < _PLOT_MIN_INTERVAL_S:
            return
        self._last_corr_plot_update = now
        if self._last_time_us is None or len(self._last_time_us) != len(corr):
            return
        self._corr_curve.setData(self._last_time_us, corr)

    def _on_rates_updated(self, payload: dict) -> None:
        now = time.monotonic()
        trend_window_s = self.spin_trend_window.value()

        rates = payload["rates"]
        fractions = payload["fractions"]
        velocities = payload["velocities"]

        for label, hz in rates.items():
            if label in self._rate_value_labels:
                self._rate_value_labels[label].setText(f"{hz:.1f} Hz")

            pct_label = self._transit_pct_labels.get(label)
            if pct_label is not None:
                fraction = fractions.get(label)    # None when threshold not set
                avg_vel = velocities.get(label)     # None when no transit events yet
                if fraction is not None:
                    splat_pct = 100.0 - fraction
                    if avg_vel is not None:
                        vel_str = (
                            f"~{avg_vel / 1000:.2f} km/s"
                            if avg_vel >= 1000
                            else f"~{avg_vel:.0f} m/s"
                        )
                        pct_label.setText(
                            f"↳  {splat_pct:.1f}% splat  ·  {fraction:.1f}% transit  ({vel_str})"
                        )
                    else:
                        pct_label.setText(
                            f"↳  {splat_pct:.1f}% splat  ·  {fraction:.1f}% transit"
                        )
                else:
                    pct_label.setText("")

            dq_t = self._trend_times.get(label)
            dq_r = self._trend_rates.get(label)
            if dq_t is None or dq_r is None:
                continue

            dq_t.append(now)
            dq_r.append(hz)
            cutoff = now - trend_window_s
            while dq_t and dq_t[0] < cutoff:
                dq_t.popleft()
                dq_r.popleft()

            curve = self._trend_curves.get(label)
            if curve is not None:
                xs = [t - now for t in dq_t]
                curve.setData(xs, list(dq_r))

        self.trend_widget.setXRange(-trend_window_s, 0, padding=0)

    def _on_trace_count(self, count: int) -> None:
        self.lbl_trace_count.setText(f"Traces:  {count}")

    def _on_status(self, msg: str) -> None:
        self._set_status(msg)

    def _on_error(self, msg: str) -> None:
        self._set_status(f"Error: {msg}")

    def _on_daq_busy(self, busy: bool) -> None:
        """Disable Run when another page holds the PicoScope."""
        self._daq_busy = busy
        if busy and self._worker is None:
            self._set_status("PicoScope in use by DAQ")
        elif not busy and self._worker is None:
            self._set_status("Idle")
        self._sync_switches()

    # ------------------------------------------------------------------
    # Tiles: summaries and -/+ steppers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_count(n: float, unit: str) -> str:
        for scale, prefix in ((1e6, "M"), (1e3, "k")):
            if n >= scale:
                return f"{n / scale:.3g} {prefix}{unit}"
        return f"{n:.3g} {unit}"

    def _derived_scope_values(self):
        """(samples per trace, sample rate in Hz) for the current Scope settings.
        Samples matches RatemeterConfig.num_samples."""
        interval_ns = _SAMPLE_INTERVALS.get(self.combo_interval.currentText(), 200)
        samples = max(1, int(self.spin_window.value() * 1e6 / interval_ns))
        return samples, 1e9 / interval_ns

    def _refresh_tiles(self, *_args) -> None:
        if not getattr(self, "_tiles_ready", False):
            return
        warn = bool(self.lbl_mf_window_warning.text())

        channel = (
            f"{self.combo_channel.currentText()}  {self.combo_coupling.currentText()}  "
            f"{self.combo_probe.currentText()}  {self.combo_range.currentText()}"
        )
        self._tiles["channel"].set_values(
            channel, ["BW limit 200 kHz"] if self.chk_bandwidth_limit.isChecked() else None
        )

        samples, rate_hz = self._derived_scope_values()
        self._tiles["scope"].set_values(
            f"{self.spin_window.value():g} ms",
            [f"Samples {self._format_count(samples, 'S')}",
             f"Rate {self._format_count(rate_hz, 'S/s')}"],
            warn=warn,
        )

        if self.chk_trigger_enable.isChecked():
            arrow = {"RISING": "↑", "FALLING": "↓"}.get(self._trigger_direction_value(), "⇅")
            trig = f"{self.spin_trigger_threshold.value():g} mV {arrow}"
        else:
            trig = "Off"
        self._tiles["trigger"].set_values(trig)
        self._tiles["captures"].set_values(f"{self.spin_captures_per_batch.value()} / batch")

        if self.combo_detection_mode.currentText() == _DETECTION_MODE_MATCHED_FILTER:
            det = f"Matched filter · {self.spin_mf_pulse.value():g} µs · {self.spin_mf_threshold.value():.2f}"
        else:
            det = _DETECTION_MODE_SIMPLE
        self._tiles["detection"].set_values(det, warn=warn)

        width = ("FWHM", "20 %", "10 %")[max(0, min(2, self.combo_width_rel_height.currentIndex()))]
        self._tiles["rates"].set_values(
            f"{self.spin_rate_avg.value()} s avg · {self.spin_trend_window.value()} s trend · {width}"
        )

    def _step_range(self, direction: int) -> None:
        idx = self.combo_range.currentIndex() + direction
        if 0 <= idx < self.combo_range.count():
            self.combo_range.setCurrentIndex(idx)

    def _step_window(self, direction: int) -> None:
        """Move the window duration to the next value on a 1-2-5 sequence."""
        value = self.spin_window.value()
        steps = [m * 10 ** e for e in range(-1, 4) for m in (1, 2, 5)]
        if direction > 0:
            target = next((s for s in steps if s > value * 1.0001), steps[-1])
        else:
            target = next((s for s in reversed(steps) if s < value * 0.9999), steps[0])
        self.spin_window.setValue(target)

    def _step_trigger(self, direction: int) -> None:
        step_mv = self._current_true_voltage_range_v() * 1000 * 0.1
        self.spin_trigger_threshold.setValue(self.spin_trigger_threshold.value() + direction * step_mv)

    # ------------------------------------------------------------------
    # Connect / Run switches
    # ------------------------------------------------------------------

    def _on_connect_switch(self, checked: bool) -> None:
        (self._on_connect_clicked if checked else self._on_disconnect_clicked)()
        self._sync_switches()

    def _on_run_switch(self, checked: bool) -> None:
        (self._on_start_clicked if checked else self._on_stop_clicked)()
        self._sync_switches()

    def _sync_switches(self) -> None:
        """Set both switches from real state (never from what was clicked)."""
        running = self._worker is not None
        connected = self._service.is_connected
        for sw, checked, enabled in (
            (self.sw_connect, connected, not running),
            (self.sw_run, running, running or (connected and not self._daq_busy)),
        ):
            sw.blockSignals(True)
            sw.setChecked(checked)
            sw.setEnabled(enabled)
            sw.blockSignals(False)

    def _set_status(self, text: str) -> None:
        """Write the status label and colour it by state (idle/running/error)."""
        self.lbl_status.setText(text)
        if text.startswith("Error"):
            color = style.BAD
        elif self._worker is not None:
            color = style.GOOD
        else:
            color = style.TXT_MUTED
        self.lbl_status.setStyleSheet(f"color: {color}; font: bold 12pt 'Segoe UI';")
        self.lbl_trace_count.setStyleSheet(f"color: {style.TXT_MUTED}; font-size: 9pt;")

    # ------------------------------------------------------------------
    # Config building
    # ------------------------------------------------------------------

    def _current_probe_factor(self) -> float:
        return PROBE_FACTORS.get(self.combo_probe.currentText(), 1.0)

    def _current_true_voltage_range_v(self) -> float:
        native_v = self.combo_range.currentData()
        if native_v is None:
            native_v = 0.02
        return native_v * self._current_probe_factor()

    def _populate_range_combo(self) -> None:
        """(Re)populate combo_range using the current probe factor. Labels are
        true (probe-scaled) volts; itemData is the native hardware range sent
        to the PicoScope."""
        preserve_native = self.combo_range.currentData() if self.combo_range.count() else None
        factor = self._current_probe_factor()
        self.combo_range.blockSignals(True)
        self.combo_range.clear()
        for native_v in NATIVE_VOLTAGE_RANGES_V:
            self.combo_range.addItem(format_voltage_label(native_v * factor), native_v)
        if preserve_native is not None:
            idx = self.combo_range.findData(preserve_native)
            self.combo_range.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_range.blockSignals(False)

    def _on_probe_changed(self) -> None:
        self._populate_range_combo()
        self._schedule_restart()

    def _build_config(self) -> RatemeterConfig:
        voltage_range_v = self.combo_range.currentData()
        if voltage_range_v is None:
            voltage_range_v = 0.02
        sample_interval_ns = _SAMPLE_INTERVALS.get(self.combo_interval.currentText(), 200)
        width_rel_height = _WIDTH_REL_HEIGHT_MAP.get(
            self.combo_width_rel_height.currentIndex(), 0.5
        )
        return RatemeterConfig(
            channel=self.combo_channel.currentText(),
            voltage_range_v=voltage_range_v,
            coupling=self.combo_coupling.currentText(),
            sample_interval_ns=sample_interval_ns,
            window_duration_ms=self.spin_window.value(),
            rate_averaging_s=float(self.spin_rate_avg.value()),
            bands=self._bands_from_table(),
            electrode_length_m=0.03302,
            width_rel_height=width_rel_height,
            bandwidth_limit_enabled=self.chk_bandwidth_limit.isChecked(),
            probe_factor=self._current_probe_factor(),
        )

    def _build_mf_config(self) -> MatchedFilterConfig:
        enabled = self.combo_detection_mode.currentText() == _DETECTION_MODE_MATCHED_FILTER
        return MatchedFilterConfig(
            enabled=enabled,
            pulse_duration_us=self.spin_mf_pulse.value(),
            pulse_min_us=self.spin_mf_pulse_min.value(),
            pulse_max_us=self.spin_mf_pulse_max.value(),
            polarity=_MF_POLARITY_MAP[self.combo_mf_polarity.currentText()],
            correlation_threshold=self.spin_mf_threshold.value(),
            min_distance_us=self.spin_mf_min_spacing.value(),
            use_empirical_template=self.chk_mf_use_empirical.isChecked(),
            empirical_template_path=self._mf_empirical_template_path,
            scale_factors=self._parse_mf_scales() or [1.0],
        )

    def _trigger_direction_value(self) -> str:
        return _TRIGGER_DIRECTIONS.get(self.combo_trigger_direction.currentText(), "RISING")

    # ------------------------------------------------------------------
    # Control enable/disable
    # ------------------------------------------------------------------

    def _set_controls_idle(self) -> None:
        self.btn_record.setEnabled(False)
        self.btn_timed_record.setEnabled(False)
        self._sync_switches()

    def _set_controls_running(self) -> None:
        self.btn_record.setEnabled(True)
        self.btn_timed_record.setEnabled(True)
        self._sync_switches()

    @staticmethod
    def _set_label_good(label: QLabel, text: str) -> None:
        label.setText(text)
        label.setStyleSheet(f"color: {style.GOOD}; font-weight: bold;")

    @staticmethod
    def _set_label_bad(label: QLabel, text: str) -> None:
        label.setText(text)
        label.setStyleSheet(f"color: {style.BAD}; font-weight: bold;")

    # ------------------------------------------------------------------
    # QSettings persistence
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        self._loading_settings = True
        try:
            self._load_settings_impl()
        finally:
            self._loading_settings = False

    def _load_settings_impl(self) -> None:
        s = self._settings

        # Probe must be restored before the range selection below, since the
        # range combo's labels/options depend on which probe is active.
        self.combo_probe.setCurrentText(s.value("ratemeter/probe", "x1", type=str))

        range_v = s.value("ratemeter/voltage_range_v", 0.02, type=float)
        idx = self.combo_range.findData(range_v)
        self.combo_range.setCurrentIndex(idx if idx >= 0 else 0)

        interval_ns = s.value("ratemeter/sample_interval_ns", 200, type=int)
        self.combo_interval.setCurrentText(
            self._label_for_value(_SAMPLE_INTERVALS, interval_ns, "200 ns")
        )

        self.spin_window.setValue(s.value("ratemeter/window_duration_ms", 5.0, type=float))
        self.combo_coupling.setCurrentText(s.value("ratemeter/coupling", "DC", type=str))
        self.chk_trigger_enable.setChecked(s.value("ratemeter/trigger_enabled", False, type=bool))
        self.spin_trigger_threshold.setValue(
            s.value("ratemeter/trigger_threshold_mv", 6.0, type=float)
        )
        self.combo_trigger_direction.setCurrentText(
            s.value("ratemeter/trigger_direction", "Rising", type=str)
        )
        self.spin_trigger_auto.setValue(s.value("ratemeter/trigger_auto_ms", 1000, type=int))
        self.spin_rate_avg.setValue(s.value("ratemeter/rate_averaging_s", 10, type=int))
        self.spin_trend_window.setValue(s.value("ratemeter/trend_window_s", 60, type=int))
        self.combo_width_rel_height.setCurrentIndex(
            s.value("ratemeter/width_rel_height_idx", 0, type=int)
        )
        self.chk_bandwidth_limit.setChecked(
            s.value("ratemeter/bandwidth_limit_enabled", False, type=bool)
        )
        self.spin_captures_per_batch.setValue(
            s.value("ratemeter/captures_per_batch", 1, type=int)
        )

        self.combo_detection_mode.setCurrentText(
            s.value("ratemeter/detection_mode", _DETECTION_MODE_SIMPLE, type=str)
        )
        self.spin_mf_pulse.setValue(s.value("ratemeter/mf_pulse_us", 85.0, type=float))
        self.spin_mf_pulse_min.setValue(s.value("ratemeter/mf_pulse_min_us", 60.0, type=float))
        self.spin_mf_pulse_max.setValue(s.value("ratemeter/mf_pulse_max_us", 110.0, type=float))
        try:
            saved_scales = json.loads(s.value("ratemeter/mf_scale_factors", "[1.0]", type=str))
            self.edit_mf_scales.setText(", ".join(f"{float(v):g}" for v in saved_scales))
        except (ValueError, TypeError):
            self.edit_mf_scales.setText("1.0")
        saved_template = s.value("ratemeter/mf_template_path", "", type=str)
        if saved_template:
            self._set_mf_template_path(saved_template)
        self.chk_mf_use_empirical.setChecked(
            s.value("ratemeter/mf_use_empirical", bool(self._mf_empirical_template_path), type=bool)
        )
        self._update_mf_scale_preview()
        self.combo_mf_polarity.setCurrentText(
            s.value("ratemeter/mf_polarity", "Positive first", type=str)
        )
        self.spin_mf_threshold.setValue(s.value("ratemeter/mf_corr_threshold", 0.35, type=float))
        self.spin_mf_min_spacing.setValue(s.value("ratemeter/mf_min_spacing_us", 50.0, type=float))

        bands_json = s.value("ratemeter/bands", "", type=str)
        self._load_bands_from_json(bands_json)

        self.spin_timed_duration.setValue(
            s.value("ratemeter/timed_recording_duration_s", 30, type=int)
        )

        self._on_trigger_enabled_changed()
        self._on_detection_mode_changed()
        self._update_plot_axes()
        self._rebuild_band_dependent_ui()
        self._refresh_tiles()

    def _load_bands_from_json(self, raw: str) -> None:
        self.table_bands.blockSignals(True)
        self.table_bands.setRowCount(0)
        try:
            items = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            items = []
        for i, entry in enumerate(items):
            row = self.table_bands.rowCount()
            self.table_bands.insertRow(row)
            try:
                low = float(entry.get("low_mv", 0.0))
                high = float(entry.get("high_mv", 0.0))
            except (TypeError, ValueError):
                low, high = 0.0, 0.0
            color = entry.get("color") or _BAND_COLORS[i % len(_BAND_COLORS)]
            transit_min_width_ns = entry.get("transit_min_width_ns")  # None if absent or null
            self._write_band_row(row, low, high, color, transit_min_width_ns)
        self.table_bands.blockSignals(False)

    @staticmethod
    def _label_for_value(mapping: dict, value, default_label: str) -> str:
        for label, v in mapping.items():
            if v == value:
                return label
        return default_label

    def _save_settings(self) -> None:
        if self._loading_settings:
            return
        s = self._settings
        s.setValue("ratemeter/probe", self.combo_probe.currentText())
        native_v = self.combo_range.currentData()
        s.setValue("ratemeter/voltage_range_v", native_v if native_v is not None else 0.02)
        s.setValue("ratemeter/sample_interval_ns", _SAMPLE_INTERVALS.get(self.combo_interval.currentText(), 200))
        s.setValue("ratemeter/window_duration_ms", self.spin_window.value())
        s.setValue("ratemeter/coupling", self.combo_coupling.currentText())
        s.setValue("ratemeter/trigger_enabled", self.chk_trigger_enable.isChecked())
        s.setValue("ratemeter/trigger_threshold_mv", self.spin_trigger_threshold.value())
        s.setValue("ratemeter/trigger_direction", self.combo_trigger_direction.currentText())
        s.setValue("ratemeter/trigger_auto_ms", self.spin_trigger_auto.value())
        s.setValue("ratemeter/rate_averaging_s", self.spin_rate_avg.value())
        s.setValue("ratemeter/trend_window_s", self.spin_trend_window.value())
        s.setValue("ratemeter/width_rel_height_idx", self.combo_width_rel_height.currentIndex())
        s.setValue("ratemeter/bandwidth_limit_enabled", self.chk_bandwidth_limit.isChecked())
        s.setValue("ratemeter/captures_per_batch", self.spin_captures_per_batch.value())

        s.setValue("ratemeter/detection_mode", self.combo_detection_mode.currentText())
        s.setValue("ratemeter/mf_pulse_us", self.spin_mf_pulse.value())
        s.setValue("ratemeter/mf_pulse_min_us", self.spin_mf_pulse_min.value())
        s.setValue("ratemeter/mf_pulse_max_us", self.spin_mf_pulse_max.value())
        s.setValue("ratemeter/mf_scale_factors", json.dumps(self._parse_mf_scales() or [1.0]))
        s.setValue("ratemeter/mf_template_path", self._mf_empirical_template_path)
        s.setValue("ratemeter/mf_use_empirical", self.chk_mf_use_empirical.isChecked())
        s.setValue("ratemeter/mf_polarity", self.combo_mf_polarity.currentText())
        s.setValue("ratemeter/mf_corr_threshold", self.spin_mf_threshold.value())
        s.setValue("ratemeter/mf_min_spacing_us", self.spin_mf_min_spacing.value())

        bands = [
            {
                "label": b.label,
                "low_mv": b.low_mv,
                "high_mv": b.high_mv,
                "color": b.color,
                "transit_min_width_ns": b.transit_min_width_ns,
            }
            for b in self._bands_from_table()
        ]
        s.setValue("ratemeter/bands", json.dumps(bands))

        s.setValue("ratemeter/timed_recording_duration_s", self.spin_timed_duration.value())

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.stop_acquisition()
        self._save_settings()
        if self._service.is_connected:
            self._service.disconnect()
        super().closeEvent(event)
