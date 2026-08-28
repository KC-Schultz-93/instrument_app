"""
ratemeter_page.py
------------------
Ratemeter GUI page: live diagnostic view for tuning ion optic voltages.

Shows how frequently particle signals occur within user-defined amplitude
bands (e.g. monomers vs. dimers vs. trimers), plus a live waveform strip for
visual confirmation. No data logging, no CDMS physics, stateless between runs.

Layout
------
Horizontal splitter:
  Left panel  (fixed 320 px, scrollable) — connection, acquisition, trigger,
                                            averaging, bands, run controls
  Right panel (expandable)               — waveform plot, live rate display,
                                            rate trend plot

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
from typing import Dict, List, Optional

import pyqtgraph as pg
from PyQt5.QtCore import Qt, QSettings, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
from instrument_app.services.daq_models import AmplitudeBand, RatemeterConfig, RatemeterEvent
from instrument_app.services.picoscope_service import PicoScopeService
from instrument_app.services.ratemeter_logger import RatemeterLogger
from instrument_app.services.ratemeter_worker import RatemeterWorker
from instrument_app.theme.style import style


# Same org/app identity as app/main.py's QSettings(APP_ORG, APP_NAME).
_APP_ORG = "JohnsonLab"
_APP_NAME = "NanoInstrumentApp"

# Voltage range options — same list as DAQPage's _VOLTAGE_RANGES.
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

_PLOT_MIN_INTERVAL_S = 0.1  # 10 Hz waveform refresh cap
_RESTART_DEBOUNCE_MS = 300

_WIDTH_REL_HEIGHT_MAP = {0: 0.5, 1: 0.2, 2: 0.1}


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

        self._settings = QSettings(_APP_ORG, _APP_NAME)

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._restart_worker)

        self._build_ui()
        self._load_settings()
        self._set_controls_idle()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._make_left_panel())
        splitter.addWidget(self._make_right_panel())
        splitter.setSizes([340, 900])
        splitter.setChildrenCollapsible(False)

        root.addWidget(splitter)

    def _make_left_panel(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(self._make_connection_group())
        layout.addWidget(self._make_acquisition_group())
        layout.addWidget(self._make_trigger_group())
        layout.addWidget(self._make_averaging_group())
        layout.addWidget(self._make_bands_group())
        layout.addWidget(self._make_transit_group())
        layout.addWidget(self._make_control_group())
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setMinimumWidth(320)
        scroll.setMaximumWidth(640)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Fixed horizontal policy: only the splitter handle should change this
        # panel's width. Without this, Qt redistributes extra space to it on
        # any layout/window resize event, snapping it toward setMaximumWidth.
        scroll.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        return scroll

    def _make_connection_group(self) -> QGroupBox:
        box = QGroupBox("PicoScope Connection")
        lay = QVBoxLayout(box)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._on_connect_clicked)

        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.clicked.connect(self._on_disconnect_clicked)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_connect)
        btn_row.addWidget(self.btn_disconnect)
        lay.addLayout(btn_row)

        self.lbl_connection = QLabel("Disconnected")
        self.lbl_connection.setAlignment(Qt.AlignCenter)
        self._set_label_bad(self.lbl_connection, "Disconnected")
        lay.addWidget(self.lbl_connection)

        return box

    def _make_acquisition_group(self) -> QGroupBox:
        box = QGroupBox("Acquisition")
        lay = QVBoxLayout(box)

        lay.addWidget(QLabel("Channel:"))
        self.combo_channel = QComboBox()
        self.combo_channel.addItems(["A", "B"])
        self.combo_channel.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_channel)

        lay.addWidget(QLabel("Window duration (ms):"))
        self.spin_window = QDoubleSpinBox()
        self.spin_window.setRange(0.1, 1000.0)
        self.spin_window.setDecimals(2)
        self.spin_window.setValue(5.0)
        self.spin_window.valueChanged.connect(self._schedule_restart)
        lay.addWidget(self.spin_window)

        lay.addWidget(QLabel("Sample interval:"))
        self.combo_interval = QComboBox()
        for label in _SAMPLE_INTERVALS:
            self.combo_interval.addItem(label)
        self.combo_interval.setCurrentText("200 ns")
        self.combo_interval.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_interval)

        lay.addWidget(QLabel("Voltage range:"))
        self.combo_range = QComboBox()
        for label in _VOLTAGE_RANGES:
            self.combo_range.addItem(label)
        self.combo_range.setCurrentText("±20 mV")
        self.combo_range.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_range)

        lay.addWidget(QLabel("Coupling:"))
        self.combo_coupling = QComboBox()
        self.combo_coupling.addItems(["DC", "AC"])
        self.combo_coupling.currentIndexChanged.connect(self._schedule_restart)
        lay.addWidget(self.combo_coupling)

        return box

    def _make_trigger_group(self) -> QGroupBox:
        box = QGroupBox("Trigger")
        lay = QVBoxLayout(box)

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

    def _make_averaging_group(self) -> QGroupBox:
        box = QGroupBox("Averaging")
        lay = QVBoxLayout(box)

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

        return box

    def _make_bands_group(self) -> QGroupBox:
        box = QGroupBox("Bands")
        lay = QVBoxLayout(box)

        self.table_bands = QTableWidget(0, 5)
        self.table_bands.setHorizontalHeaderLabels(
            ["#", "Low (mV)", "High (mV)", "Color", "Min width (ns)"]
        )
        header = self.table_bands.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for col in (1, 2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.Stretch)
        self.table_bands.setFixedHeight(160)
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

    def _make_control_group(self) -> QGroupBox:
        box = QGroupBox("Run")
        lay = QVBoxLayout(box)

        self.btn_start = QPushButton("Start")
        self.btn_start.clicked.connect(self._on_start_clicked)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self._on_stop_clicked)

        self.lbl_status = QLabel("Idle")
        self.lbl_status.setAlignment(Qt.AlignCenter)

        self.lbl_trace_count = QLabel("Traces:  0")

        lay.addWidget(self.btn_start)
        lay.addWidget(self.btn_stop)
        lay.addWidget(self.lbl_status)
        lay.addWidget(self.lbl_trace_count)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        lay.addWidget(sep)

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

        return box

    def _make_transit_group(self) -> QGroupBox:
        box = QGroupBox("Transit Discrimination")
        lay = QVBoxLayout(box)

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
            "Signals below the threshold are counted as splat."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {style.TXT_MUTED}; font-size: 9pt;")
        lay.addWidget(note)

        return box

    def _make_right_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(0)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(self._make_waveform_plot())
        vsplit.addWidget(self._make_rates_frame())
        vsplit.addWidget(self._make_trend_plot())
        vsplit.setSizes([320, 150, 250])

        lay.addWidget(vsplit)
        return panel

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

        return self.plot_widget

    def _make_rates_frame(self) -> QWidget:
        self.rates_frame = QFrame()
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
        voltage_range_v = _VOLTAGE_RANGES.get(self.combo_range.currentText(), 0.02)
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

        self._start_worker(config)
        self.channels.daq_busy.emit(True)
        self._set_controls_running()
        self.lbl_status.setText("Running")

    def _on_stop_clicked(self) -> None:
        self.stop_acquisition()

    def stop_acquisition(self) -> None:
        """Stop the worker gracefully. Safe to call even if not running."""
        self._stop_recording()
        self.btn_record.setChecked(False)

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
        self.lbl_status.setText("Stopped")

    def _start_worker(self, config: RatemeterConfig) -> None:
        trigger_enabled = self.chk_trigger_enable.isChecked()
        trigger_threshold_v = self.spin_trigger_threshold.value() / 1000.0
        trigger_direction = self._trigger_direction_value()

        self._worker = RatemeterWorker(
            self._service, config, trigger_enabled, trigger_threshold_v, trigger_direction
        )
        self._worker.rates_updated.connect(self._on_rates_updated)
        self._worker.waveform_ready.connect(self._on_waveform_ready)
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

        self._start_worker(config)
        self.lbl_status.setText("Running")

    def _schedule_restart(self, *_args) -> None:
        self._update_plot_axes()
        self._update_trigger_line()
        self._save_settings()
        if self._worker is not None:
            self._debounce_timer.start(_RESTART_DEBOUNCE_MS)

    def _on_trigger_enabled_changed(self, _state=None) -> None:
        enabled = self.chk_trigger_enable.isChecked()
        self.spin_trigger_threshold.setEnabled(enabled)
        self.combo_trigger_direction.setEnabled(enabled)
        self.spin_trigger_auto.setEnabled(enabled)
        self._schedule_restart()

    # ------------------------------------------------------------------
    # Worker signal slots (main thread)
    # ------------------------------------------------------------------

    def _on_waveform_ready(self, record) -> None:
        now = time.monotonic()
        if now - self._last_plot_update < _PLOT_MIN_INTERVAL_S:
            return
        self._last_plot_update = now

        time_us = record.time_ns / 1e3
        voltage_mv = record.voltage * 1000
        self._plot_item.setData(time_us, voltage_mv)

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
        self.lbl_status.setText(msg)

    def _on_error(self, msg: str) -> None:
        self.lbl_status.setText(f"Error: {msg}")

    def _on_daq_busy(self, busy: bool) -> None:
        """Disable Start when another page holds the PicoScope."""
        self._daq_busy = busy
        if busy and self._worker is None:
            self.btn_start.setEnabled(False)
            self.lbl_status.setText("PicoScope in use by DAQ")
        elif not busy and self._worker is None:
            self.btn_start.setEnabled(self._service.is_connected)
            self.lbl_status.setText("Idle")

    # ------------------------------------------------------------------
    # Config building
    # ------------------------------------------------------------------

    def _build_config(self) -> RatemeterConfig:
        voltage_range_v = _VOLTAGE_RANGES.get(self.combo_range.currentText(), 0.02)
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
        )

    def _trigger_direction_value(self) -> str:
        return _TRIGGER_DIRECTIONS.get(self.combo_trigger_direction.currentText(), "RISING")

    # ------------------------------------------------------------------
    # Control enable/disable
    # ------------------------------------------------------------------

    def _set_controls_idle(self) -> None:
        connected = self._service.is_connected
        self.btn_connect.setEnabled(not connected)
        self.btn_disconnect.setEnabled(connected)
        self.btn_start.setEnabled(connected and not self._daq_busy)
        self.btn_stop.setEnabled(False)
        self.btn_record.setEnabled(False)

    def _set_controls_running(self) -> None:
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_record.setEnabled(True)

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
        s = self._settings

        range_v = s.value("ratemeter/voltage_range_v", 0.02, type=float)
        self.combo_range.setCurrentText(self._label_for_value(_VOLTAGE_RANGES, range_v, "±20 mV"))

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

        bands_json = s.value("ratemeter/bands", "", type=str)
        self._load_bands_from_json(bands_json)

        self._on_trigger_enabled_changed()
        self._update_plot_axes()
        self._rebuild_band_dependent_ui()

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
        s = self._settings
        s.setValue("ratemeter/voltage_range_v", _VOLTAGE_RANGES.get(self.combo_range.currentText(), 0.02))
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

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.stop_acquisition()
        self._save_settings()
        if self._service.is_connected:
            self._service.disconnect()
        super().closeEvent(event)
