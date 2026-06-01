import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QComboBox, QGroupBox, QSplitter, QApplication, QMainWindow,
                             QCheckBox, QSpinBox)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QPalette, QColor
import pyqtgraph as pg
import numpy as np
from scipy.ndimage import uniform_filter1d

from Services.SerialComms import ArduinoSerialComms
from Services.Channels import AppChannels
from Services.CustomWidgets import QGaugeDisplay, QPumpControl
from Services.PressureLogger import PressureLogger
from Pages.maintenance_page import MaintenanceModeDialog
from UI.theme import style, theme_mgr

# Configure PyQtGraph defaults
pg.setConfigOptions(antialias=True)


class LogPressureAxisItem(pg.AxisItem):
    """Custom axis that displays pressure values in scientific notation on a log scale.

    This axis expects data to be pre-transformed to log10 values (not raw pressure).
    It displays the tick labels as scientific notation pressure values.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Disable automatic SI prefix scaling
        self.enableAutoSIPrefix(False)
        self.setGrid(200)  # Enable grid by default

    def tickStrings(self, values, scale, spacing):
        """Convert log10 values back to scientific notation strings."""
        strings = []
        for v in values:
            # v is log10(pressure), convert back to actual pressure
            try:
                pressure = 10 ** v
                # Check if this is a "nice" power of 10 (integer exponent)
                # or an intermediate value (2, 3, 5 multipliers)
                exponent = int(np.floor(v))
                mantissa = pressure / (10 ** exponent)

                # If mantissa is close to 1, 2, 3, or 5, show clean format
                if abs(mantissa - round(mantissa)) < 0.01 and round(mantissa) in [1, 2, 3, 5, 10]:
                    mantissa_int = int(round(mantissa))
                    if mantissa_int == 1:
                        strings.append(f"1e{exponent:+d}")
                    elif mantissa_int == 10:
                        strings.append(f"1e{exponent+1:+d}")
                    else:
                        strings.append(f"{mantissa_int}e{exponent:+d}")
                else:
                    # For other values, use 1 decimal precision
                    strings.append(f"{pressure:.1e}")
            except (OverflowError, ValueError):
                strings.append("")
        return strings

    def tickValues(self, minVal, maxVal, size):
        """Generate tick values at powers of 10 and intermediate points."""
        # Handle edge cases - fall back to parent implementation if invalid
        if not np.isfinite(minVal) or not np.isfinite(maxVal):
            return super().tickValues(minVal, maxVal, size)
        if minVal >= maxVal:
            return super().tickValues(minVal, maxVal, size)

        range_size = maxVal - minVal
        start_exp = int(np.floor(minVal))
        end_exp = int(np.ceil(maxVal))

        # Sanity check - if range is unreasonable, fall back to default
        if end_exp - start_exp > 50 or end_exp - start_exp < 0:
            return super().tickValues(minVal, maxVal, size)

        # Limit the number of ticks to prevent performance issues
        if end_exp - start_exp > 15:
            # Wide range - use larger step
            step = max(1, (end_exp - start_exp) // 8)
            major_ticks = [float(exp) for exp in range(start_exp, end_exp + 1, step)
                          if minVal <= exp <= maxVal]
            return [(float(step), major_ticks)] if major_ticks else super().tickValues(minVal, maxVal, size)

        # Major ticks at each power of 10
        major_ticks = [float(exp) for exp in range(start_exp, end_exp + 1)
                      if minVal <= exp <= maxVal]

        # Minor ticks at 2, 3, 5 within each decade
        minor_ticks = []
        for exp in range(start_exp - 1, end_exp + 1):
            for mult in [2, 3, 5]:
                tick_val = exp + np.log10(mult)
                if minVal <= tick_val <= maxVal:
                    minor_ticks.append(tick_val)

        # For very narrow ranges (less than 1 decade), add finer ticks
        fine_ticks = []
        if range_size < 1.0:
            # Add ticks at 1.5, 2.5, 3.5, 4, 4.5, 6, 7, 8, 9
            for exp in range(start_exp - 1, end_exp + 1):
                for mult in [1.5, 2.5, 3.5, 4, 4.5, 6, 7, 8, 9]:
                    tick_val = exp + np.log10(mult)
                    if minVal <= tick_val <= maxVal:
                        fine_ticks.append(tick_val)

        # Return as [(spacing, [tick_values]), ...]
        result = []
        if major_ticks:
            result.append((1.0, major_ticks))
        if minor_ticks:
            result.append((0.5, minor_ticks))
        if fine_ticks:
            result.append((0.2, fine_ticks))

        # Always return something - fall back to parent if we have nothing
        return result if result else super().tickValues(minVal, maxVal, size)


_DEMO_PORT_LABEL = "--- TEST MODE (Simulated) ---"


class PressurePage(QWidget):
    def __init__(self, channels=None, parent=None):
        super().__init__(parent)
        self.channels = channels or AppChannels()
        self.serial = ArduinoSerialComms(self.channels)

        self.connected = False

        self._demo_mode = False
        self._demo_tick_count = 0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._demo_tick)

        self.time_data = deque(maxlen=43200)
        self.foreline_data = deque(maxlen=43200)
        self.uhv_data = deque(maxlen=43200)
        self.start_time = datetime.now()

        self.time_window_hours = 3
        self.current_graph = "foreline"

        self.maintenance_dialog = None

        # Pressure file logger for long-term storage
        self.pressure_logger = PressureLogger()

        # Chart update throttling (update every 5 seconds to prevent UI lag)
        self._last_chart_update = 0
        self._chart_update_interval = 5  # seconds

        # Smoothed trend line settings
        self.show_smoothed = False
        self.smoothed_plot_item = None
        self.show_only_smoothed = False  # When True, hide raw data and show only smoothed line

        # CSV data cache for hybrid approach
        self._csv_cache = {}
        self._csv_cache_timestamp = None
        self._csv_cache_validity = 300  # Cache validity in seconds (5 minutes)

        self.init_ui()
        self._connect_channels()

    def _connect_channels(self):
        self.channels.connection_changed.connect(self._on_connection_changed)
        self.channels.data_received.connect(self._on_data_received)
        self.channels.error.connect(self._on_error)

    def init_ui(self):
        main_layout = QVBoxLayout(self)

        connection_panel = self.create_connection_panel()
        main_layout.addWidget(connection_panel)

        splitter = QSplitter(Qt.Horizontal)
        left_panel = self.create_left_panel()
        right_panel = self.create_chart_panel()
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter)

    def create_connection_panel(self):
        group = QGroupBox()
        layout = QHBoxLayout()

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(300)
        self.refresh_ports()
        layout.addWidget(self.port_combo)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_ports)
        refresh_btn.setStyleSheet(self._button_style())
        layout.addWidget(refresh_btn)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.connect_btn.setStyleSheet(self._button_style())
        layout.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.disconnect_serial)
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.setStyleSheet(self._button_style())
        layout.addWidget(self.disconnect_btn)

        layout.addStretch()

        layout.addWidget(QLabel("STATUS:"))
        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet(
            f"padding: 5px 10px; background-color: {style.BTN_BG}; "
            f"color: {style.TXT}; border-radius: 3px;"
        )
        layout.addWidget(self.status_label)

        group.setLayout(layout)
        group.setMaximumHeight(70)  # Prevent vertical expansion when window maximizes
        return group

    def create_left_panel(self):
        widget = QWidget()
        layout = QVBoxLayout()

        self.foreline_label = QGaugeDisplay("Foreline Pressure", "0.000", "TORR")
        layout.addWidget(self.foreline_label)

        self.uhv_label = QGaugeDisplay("UHV Pressure", "0.00e+00", "TORR")
        layout.addWidget(self.uhv_label)

        state_group = QGroupBox("System State")
        state_layout = QVBoxLayout()
        self.state_label = QLabel("IDLE")
        self.state_label.setStyleSheet(
            f"color: {style.GOOD}; font-size: 14px; font-weight: bold;"
        )
        state_layout.addWidget(self.state_label)
        self.fault_label = QLabel("")
        self.fault_label.setStyleSheet(
            f"color: {style.BAD}; font-size: 12px; font-weight: bold;"
        )
        state_layout.addWidget(self.fault_label)
        state_group.setLayout(state_layout)
        layout.addWidget(state_group)

        self.tg60_group = QPumpControl("TG60")
        self.tg60_group.runClicked.connect(lambda: self.send_command('S'))
        self.tg60_group.stopClicked.connect(lambda: self.send_command('X'))
        layout.addWidget(self.tg60_group)

        self.tg220_group = QPumpControl("TG220")
        self.tg220_group.runClicked.connect(lambda: self.send_command('S'))
        self.tg220_group.stopClicked.connect(lambda: self.send_command('X'))
        layout.addWidget(self.tg220_group)

        # System Startup/Shutdown buttons
        system_control_group = QGroupBox("System Control")
        system_control_layout = QHBoxLayout()

        self.startup_btn = QPushButton("STARTUP")
        self.startup_btn.setStyleSheet(
            f"background-color: {style.GOOD}; color: {style.TXT_STRONG}; padding: 10px; "
            f"font-weight: bold; border-radius: 5px; border: 1px solid {style.BTN_BORDER};"
        )
        self.startup_btn.clicked.connect(lambda: self.send_command_with_feedback('S', self.startup_btn))
        system_control_layout.addWidget(self.startup_btn)

        self.shutdown_btn = QPushButton("SHUTDOWN")
        self.shutdown_btn.setStyleSheet(
            f"background-color: {style.BAD}; color: {style.TXT_STRONG}; padding: 10px; "
            f"font-weight: bold; border-radius: 5px; border: 1px solid {style.BTN_BORDER};"
        )
        self.shutdown_btn.clicked.connect(lambda: self.send_command_with_feedback('X', self.shutdown_btn))
        system_control_layout.addWidget(self.shutdown_btn)

        system_control_group.setLayout(system_control_layout)
        layout.addWidget(system_control_group)

        # Reset buttons group
        reset_group = QGroupBox("Fault Reset")
        reset_layout = QHBoxLayout()

        self.reset_hornet_btn = QPushButton("CLEAR HORNET FAULT")
        self.reset_hornet_btn.setStyleSheet(
            f"background-color: {style.BTN_BG}; color: {style.TXT}; padding: 10px; "
            f"font-weight: bold; border-radius: 5px; border: 1px solid {style.BTN_BORDER};"
        )
        self.reset_hornet_btn.clicked.connect(lambda: self.send_command_with_feedback('H', self.reset_hornet_btn))
        reset_layout.addWidget(self.reset_hornet_btn)

        self.reset_all_btn = QPushButton("RESET ALL")
        self.reset_all_btn.setStyleSheet(
            f"background-color: {style.BTN_BG}; color: {style.TXT}; padding: 10px; "
            f"font-weight: bold; border-radius: 5px; border: 1px solid {style.BTN_BORDER};"
        )
        self.reset_all_btn.clicked.connect(lambda: self.send_command_with_feedback('R', self.reset_all_btn))
        reset_layout.addWidget(self.reset_all_btn)

        reset_group.setLayout(reset_layout)
        layout.addWidget(reset_group)

        maint_btn = QPushButton("MAINTENANCE")
        maint_btn.setStyleSheet(
            f"background-color: {style.BTN_BG}; color: {style.TXT_STRONG}; padding: 10px; "
            f"font-weight: bold; border-radius: 5px; border: 1px solid {style.BTN_BORDER};"
        )
        maint_btn.clicked.connect(self.open_maintenance_dialog)
        layout.addWidget(maint_btn)

        layout.addStretch()

        widget.setLayout(layout)
        widget.setMaximumWidth(280)
        return widget

    def create_chart_panel(self):
        widget = QWidget()
        layout = QVBoxLayout()

        # Create PyQtGraph widget with time axis and custom log pressure axis
        time_axis = pg.DateAxisItem(orientation='bottom')
        self.pressure_axis = LogPressureAxisItem(orientation='left')
        self.plot_widget = pg.PlotWidget(axisItems={'bottom': time_axis, 'left': self.pressure_axis})
        self.plot_widget.setMinimumHeight(400)
        self.plot_widget.setLabel('bottom', 'Time')
        # Set left axis label directly on our custom axis to preserve it
        self.pressure_axis.setLabel('Pressure (Torr)')

        # Apply theme colors
        self.plot_widget.setBackground(style.PLOT_BG)
        plot_item = self.plot_widget.getPlotItem()
        plot_item.getAxis('bottom').setPen(pg.mkPen(style.BTN_BORDER))
        self.pressure_axis.setPen(pg.mkPen(style.BTN_BORDER))
        plot_item.getAxis('bottom').setTextPen(pg.mkPen(style.TXT))
        self.pressure_axis.setTextPen(pg.mkPen(style.TXT))

        # Enable grid lines with good visibility
        plot_item.showGrid(x=True, y=True, alpha=0.5)

        plot_item.titleLabel.item.setDefaultTextColor(pg.mkColor(style.TXT))

        # Enable pan/zoom
        self.plot_widget.setMouseEnabled(x=True, y=True)

        # Store reference for updates
        self.plot_data_item = None

        layout.addWidget(self.plot_widget)
        self.update_chart()

        controls_layout = QVBoxLayout()

        graph_type_layout = QHBoxLayout()
        graph_type_layout.addWidget(QLabel("Graph Type:"))

        self.foreline_radio = QPushButton("Foreline")
        self.foreline_radio.setCheckable(True)
        self.foreline_radio.setChecked(True)
        self.foreline_radio.clicked.connect(lambda: self.change_graph_type("foreline"))
        self.foreline_radio.setStyleSheet(self._toggle_style())
        graph_type_layout.addWidget(self.foreline_radio)

        self.uhv_radio = QPushButton("UHV")
        self.uhv_radio.setCheckable(True)
        self.uhv_radio.clicked.connect(lambda: self.change_graph_type("uhv"))
        self.uhv_radio.setStyleSheet(self._toggle_style())
        graph_type_layout.addWidget(self.uhv_radio)

        graph_type_layout.addStretch()
        controls_layout.addLayout(graph_type_layout)

        time_window_layout = QHBoxLayout()
        time_window_layout.addWidget(QLabel("Time Window:"))

        self.time_3h_btn = QPushButton("3 Hours")
        self.time_3h_btn.setCheckable(True)
        self.time_3h_btn.setChecked(True)
        self.time_3h_btn.clicked.connect(lambda: self.change_time_window(3))
        time_window_layout.addWidget(self.time_3h_btn)

        self.time_6h_btn = QPushButton("6 Hours")
        self.time_6h_btn.setCheckable(True)
        self.time_6h_btn.clicked.connect(lambda: self.change_time_window(6))
        time_window_layout.addWidget(self.time_6h_btn)

        self.time_12h_btn = QPushButton("12 Hours")
        self.time_12h_btn.setCheckable(True)
        self.time_12h_btn.clicked.connect(lambda: self.change_time_window(12))
        time_window_layout.addWidget(self.time_12h_btn)

        time_btn_style = self._toggle_style(selected_color=style.GOOD)
        self.time_3h_btn.setStyleSheet(time_btn_style)
        self.time_6h_btn.setStyleSheet(time_btn_style)
        self.time_12h_btn.setStyleSheet(time_btn_style)

        time_window_layout.addSpacing(20)

        # Custom time window input
        time_window_layout.addWidget(QLabel("Custom Hours:"))
        self.custom_hours_input = QSpinBox()
        self.custom_hours_input.setMinimum(1)
        self.custom_hours_input.setMaximum(720)  # 30 days max
        self.custom_hours_input.setValue(3)
        self.custom_hours_input.setStyleSheet(
            f"QSpinBox {{ background-color: {style.BTN_BG}; color: {style.TXT}; padding: 6px; "
            f"border-radius: 4px; border: 1px solid {style.BTN_BORDER}; width: 60px; }}"
            f"QSpinBox::up-button {{ width: 16px; }} QSpinBox::down-button {{ width: 16px; }}"
        )
        self.custom_hours_input.valueChanged.connect(self.on_custom_hours_changed)
        time_window_layout.addWidget(self.custom_hours_input)

        time_window_layout.addStretch()

        # Smoothed trend line checkbox
        self.smooth_checkbox = QCheckBox("Show Smoothed Trend")
        self.smooth_checkbox.setChecked(False)
        self.smooth_checkbox.stateChanged.connect(self.toggle_smoothed_line)
        self.smooth_checkbox.setStyleSheet(
            f"QCheckBox {{ color: {style.TXT}; padding: 8px; }}"
            f"QCheckBox::indicator {{ width: 18px; height: 18px; }}"
            f"QCheckBox::indicator:checked {{ background-color: {style.GOOD}; border: 1px solid {style.BTN_BORDER}; border-radius: 3px; }}"
            f"QCheckBox::indicator:unchecked {{ background-color: {style.BTN_BG}; border: 1px solid {style.BTN_BORDER}; border-radius: 3px; }}"
        )
        time_window_layout.addWidget(self.smooth_checkbox)

        # Smoothed only checkbox (show only smoothed line, hide raw data)
        self.smooth_only_checkbox = QCheckBox("Smoothed Only")
        self.smooth_only_checkbox.setChecked(False)
        self.smooth_only_checkbox.stateChanged.connect(self.toggle_smoothed_only)
        self.smooth_only_checkbox.setStyleSheet(
            f"QCheckBox {{ color: {style.TXT}; padding: 8px; }}"
            f"QCheckBox::indicator {{ width: 18px; height: 18px; }}"
            f"QCheckBox::indicator:checked {{ background-color: {style.GOOD}; border: 1px solid {style.BTN_BORDER}; border-radius: 3px; }}"
            f"QCheckBox::indicator:unchecked {{ background-color: {style.BTN_BG}; border: 1px solid {style.BTN_BORDER}; border-radius: 3px; }}"
        )
        time_window_layout.addWidget(self.smooth_only_checkbox)

        reset_view_btn = QPushButton("Clear Data")
        reset_view_btn.clicked.connect(self.reset_view)
        reset_view_btn.setStyleSheet(
            f"background-color: {style.BTN_BG}; color: {style.TXT_STRONG}; padding: 8px 16px; "
            f"border-radius: 4px; border: 1px solid {style.BTN_BORDER};"
        )
        time_window_layout.addWidget(reset_view_btn)

        controls_layout.addLayout(time_window_layout)
        layout.addLayout(controls_layout)

        widget.setLayout(layout)
        return widget

    def refresh_ports(self):
        self.port_combo.clear()
        self.port_combo.addItem(_DEMO_PORT_LABEL)
        for port in self.serial.refresh_ports():
            self.port_combo.addItem(port)

    def _get_csv_file_paths(self, pressure_type, start_date, end_date):
        """Get list of CSV file paths for a given date range and pressure type.
        
        Args:
            pressure_type: 'uhv' or 'foreline'
            start_date: datetime.date object
            end_date: datetime.date object
        
        Returns:
            List of Path objects that exist
        """
        base_dir = Path(self.pressure_logger.base_dir)
        files = []
        
        current = start_date
        while current <= end_date:
            year_str = str(current.year)
            month_str = f"{current.month:02d}"
            date_str = f"{current.year}_{current.month:02d}_{current.day:02d}"
            
            if pressure_type == 'uhv':
                file_name = f"{date_str}_UHV_Pressure.csv"
            else:  # foreline
                file_name = f"{date_str}_Foreline_Pressure.csv"
            
            file_path = base_dir / year_str / month_str / file_name
            
            if file_path.exists():
                files.append(file_path)
            
            current += timedelta(days=1)
        
        return files
    
    def _load_csv_data(self, pressure_type, start_time_unix, end_time_unix):
        """Load pressure data from CSV files for a given time range.
        
        Args:
            pressure_type: 'uhv' or 'foreline'
            start_time_unix: Unix timestamp (seconds)
            end_time_unix: Unix timestamp (seconds)
        
        Returns:
            Tuple of (times_array, pressures_array) as numpy arrays, or (None, None) if no data
        """
        times_list = []
        pressures_list = []
        
        start_datetime = datetime.fromtimestamp(start_time_unix)
        end_datetime = datetime.fromtimestamp(end_time_unix)
        start_date = start_datetime.date()
        end_date = end_datetime.date()
        
        files = self._get_csv_file_paths(pressure_type, start_date, end_date)
        
        for file_path in files:
            try:
                with open(file_path, 'r') as f:
                    lines = f.readlines()
                    # Skip header
                    for line in lines[1:]:
                        parts = line.strip().split(',')
                        if len(parts) >= 3:
                            try:
                                timestamp_str = parts[0]
                                pressure_str = parts[2]
                                
                                # Parse ISO format timestamp
                                dt = datetime.fromisoformat(timestamp_str)
                                unix_time = dt.timestamp()
                                pressure = float(pressure_str)
                                
                                # Filter to requested time range
                                if start_time_unix <= unix_time <= end_time_unix:
                                    times_list.append(unix_time)
                                    pressures_list.append(pressure)
                            except (ValueError, IndexError):
                                continue
            except (IOError, OSError):
                continue
        
        if times_list:
            return np.array(times_list), np.array(pressures_list)
        else:
            return None, None
    
    def _get_data_for_window(self, pressure_type, time_window_seconds):
        """Get pressure data for the given time window using hybrid approach.
        
        If the window fits in memory, use deque data. If it extends beyond,
        load additional data from CSV files.
        
        Args:
            pressure_type: 'uhv' or 'foreline'
            time_window_seconds: How many seconds back to show
        
        Returns:
            Tuple of (times_array, pressures_array) as numpy arrays
        """
        if len(self.time_data) < 2:
            return None, None
        
        current_time = time.time()
        deque_oldest_time = self.time_data[0] if len(self.time_data) > 0 else current_time
        deque_age_seconds = current_time - deque_oldest_time
        
        # Select data source based on type
        if pressure_type == 'foreline':
            deque_data = self.foreline_data
        else:
            deque_data = self.uhv_data
        
        # If window fits entirely in deque, use only deque (fast path)
        if time_window_seconds <= deque_age_seconds:
            times = np.array(self.time_data)
            pressures = np.array(deque_data)
            mask = times >= (current_time - time_window_seconds)
            return times[mask], pressures[mask]
        
        # Window extends beyond deque - need to load from CSV
        csv_start_time = current_time - time_window_seconds
        
        # Load CSV data from start of window to oldest deque entry
        csv_times, csv_pressures = self._load_csv_data(
            pressure_type,
            csv_start_time,
            deque_oldest_time - 1  # Avoid overlap
        )
        
        # Combine CSV data (older) with deque data (newer)
        times_list = []
        pressures_list = []
        
        if csv_times is not None:
            times_list.extend(csv_times)
            pressures_list.extend(csv_pressures)
        
        times_list.extend(self.time_data)
        pressures_list.extend(deque_data)
        
        if times_list:
            return np.array(times_list), np.array(pressures_list)
        else:
            return None, None


    def toggle_connection(self):
        if self.connected:
            return
        port_text = self.port_combo.currentText()
        if not port_text:
            self.status_label.setText("No port selected")
            return
        if port_text == _DEMO_PORT_LABEL:
            self._start_demo_mode()
            return
        port_name = port_text.split(' ')[0]
        self.serial.open_port(port_name)

    def disconnect_serial(self):
        if self._demo_mode:
            self._stop_demo_mode()
        else:
            self.serial.close_port()

    def _start_demo_mode(self):
        self._demo_mode = True
        self._demo_tick_count = 0
        self._prepopulate_demo_data()
        self.channels.connection_changed.emit(True, _DEMO_PORT_LABEL)
        self._demo_timer.start(1000)

    def _stop_demo_mode(self):
        self._demo_timer.stop()
        self._demo_mode = False
        self.channels.connection_changed.emit(False, "")

    def _prepopulate_demo_data(self):
        """Fill deques with 3 hours of simulated pressure history so trend lines appear immediately."""
        rng = np.random.default_rng(42)
        now = time.time()
        num_points = 1080  # 3 h at 10-second intervals

        for i in range(num_points):
            t = now - (num_points - i) * 10
            phase = i / num_points * 2 * np.pi

            fore = 0.8 + 0.015 * np.sin(phase) + rng.normal(0, 0.004)
            uhv = 5e-9 * (1.0 + 0.08 * np.sin(phase * 0.7) + rng.normal(0, 0.025))
            uhv = max(uhv, 1e-12)

            self.time_data.append(t)
            self.foreline_data.append(fore)
            self.uhv_data.append(uhv)

        self.start_time = datetime.fromtimestamp(now - num_points * 10)

    def _demo_tick(self):
        """Emit one simulated data point each second."""
        self._demo_tick_count += 1
        phase = (self._demo_tick_count / 60.0) * 2 * np.pi

        fore = 0.8 + 0.015 * np.sin(phase) + np.random.normal(0, 0.004)
        uhv = 5e-9 * (1.0 + 0.08 * np.sin(phase * 0.7) + np.random.normal(0, 0.025))
        uhv = max(uhv, 1e-12)

        data = {
            "timestamp_ms": self._demo_tick_count * 1000,
            "uhv_torr": uhv,
            "fore_torr": fore,
            "state": "RUN",
            "tg60_ok": "OK",
            "tg220_ok": "OK",
            "rel_tg60": 1,
            "rel_tg220": 1,
            "rel_hornet": 1,
            "rel_test": 0,
            "fault_hornet": 0,
            "fault_system": 0,
            "maint": 0,
        }
        self.channels.data_received.emit(data)

    def _on_connection_changed(self, connected, port_name):
        self.connected = connected
        if connected:
            self.status_label.setText("Connected")
            self.status_label.setStyleSheet(
                f"padding: 5px 10px; background-color: {style.GOOD}; "
                f"color: {style.TXT_STRONG}; border-radius: 3px;"
            )
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            # Don't clear data on reconnection - keep historical data
            # Only clear if this is the first connection (no data yet)
            if len(self.time_data) == 0:
                self.start_time = datetime.now()
        else:
            self.status_label.setText("Not connected")
            self.status_label.setStyleSheet(
                f"padding: 5px 10px; background-color: {style.BTN_BG}; "
                f"color: {style.TXT}; border-radius: 3px;"
            )
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)

    def _on_error(self, message):
        self.status_label.setText(f"Error: {message}")
        self.status_label.setStyleSheet(
            f"padding: 5px 10px; background-color: {style.BAD}; "
            f"color: {style.TXT_STRONG}; border-radius: 3px;"
        )

    def _on_data_received(self, data):
        timestamp_ms = data["timestamp_ms"]
        uhv_torr = data["uhv_torr"]
        fore_torr = data["fore_torr"]
        state = data["state"]
        tg60_ok = data["tg60_ok"]
        tg220_ok = data["tg220_ok"]
        fault_hornet = data.get("fault_hornet", 0)
        fault_system = data.get("fault_system", 0)
        maint = data["maint"]
        rel_tg60 = data.get("rel_tg60", 0)  # Relay state for foreline gauge
        rel_tg220 = data.get("rel_tg220", 0)  # Relay state for UHV gauge

        # Store Unix timestamp for time-of-day display
        current_timestamp = time.time()
        self.time_data.append(current_timestamp)
        self.foreline_data.append(fore_torr)
        self.uhv_data.append(uhv_torr)

        # Log to file for long-term storage (convert timestamp to elapsed minutes for logger)
        elapsed_minutes = (current_timestamp - self.start_time.timestamp()) / 60.0
        self.pressure_logger.log_pressure(elapsed_minutes, uhv_torr, fore_torr)

        # Update gauge displays (always - lightweight operation)
        self.update_displays(uhv_torr, fore_torr, state, tg60_ok, tg220_ok, fault_hornet, fault_system, maint, rel_tg60, rel_tg220)

        # Update chart (throttled to prevent UI lag with large datasets)
        now = time.time()
        if now - self._last_chart_update >= self._chart_update_interval:
            self.update_chart()
            self._last_chart_update = now

    def update_displays(self, uhv_torr, fore_torr, state, tg60_ok, tg220_ok, fault_hornet, fault_system, maint, rel_tg60=0, rel_tg220=0):
        # Use scientific notation for foreline if pressure is very small or very large
        if fore_torr < 0.01 or fore_torr > 1000:
            self.foreline_label.set_value(f"{fore_torr:.2e}")
        else:
            self.foreline_label.set_value(f"{fore_torr:.3f}")
        self.uhv_label.set_value(f"{uhv_torr:.2e}")

        # Update gauge status indicators based on relay energization
        # rel_tg60 controls the foreline gauge, rel_tg220 controls the UHV gauge
        self.foreline_label.set_status(bool(rel_tg60))
        self.uhv_label.set_status(bool(rel_tg220))

        self.state_label.setText(state)

        # Display fault status with details
        fault_messages = []
        if fault_hornet:
            fault_messages.append("HORNET FAULT")
        if fault_system:
            fault_messages.append("SYSTEM FAULT")

        if fault_messages:
            self.fault_label.setText(" | ".join(fault_messages))
        else:
            self.fault_label.setText("")

        self.tg60_group.set_status(tg60_ok)
        self.tg220_group.set_status(tg220_ok)

        # Update maintenance dialog state if it's open
        if self.maintenance_dialog is not None:
            if maint:
                self.maintenance_dialog.enable_maint_controls()
            else:
                self.maintenance_dialog.disable_maint_controls()

    def update_chart(self):
        """Update the PyQtGraph plot with current data using hybrid approach.
        
        Uses in-memory deque for recent data (< 12 hours), loads from CSV for older data.
        """

        # Handle insufficient data
        if len(self.time_data) < 2:
            self.plot_widget.setTitle("Waiting for data...")
            if self.plot_data_item is not None:
                self.plot_widget.removeItem(self.plot_data_item)
                self.plot_data_item = None
            if self.smoothed_plot_item is not None:
                self.plot_widget.removeItem(self.smoothed_plot_item)
                self.smoothed_plot_item = None
            return

        # Get data using hybrid approach (deque + CSV)
        time_window_seconds = self.time_window_hours * 3600
        
        if self.current_graph == "foreline":
            times_filtered, data_filtered = self._get_data_for_window("foreline", time_window_seconds)
            title_text = f"Foreline Pressure (Last {self.time_window_hours}h)"
            line_color = style.GOOD
        else:
            times_filtered, data_filtered = self._get_data_for_window("uhv", time_window_seconds)
            title_text = f"UHV Pressure (Last {self.time_window_hours}h)"
            line_color = style.BAD

        # Handle case where no data is available
        if times_filtered is None or len(times_filtered) < 2:
            self.plot_widget.setTitle("No data available")
            if self.plot_data_item is not None:
                self.plot_widget.removeItem(self.plot_data_item)
                self.plot_data_item = None
            if self.smoothed_plot_item is not None:
                self.plot_widget.removeItem(self.smoothed_plot_item)
                self.smoothed_plot_item = None
            return

        # Transform pressure data to log10 for proper logarithmic display
        # Clamp minimum to avoid log(0) issues
        data_log = np.log10(np.maximum(data_filtered, 1e-12))

        # Update title
        self.plot_widget.setTitle(title_text)

        # Remove old plots if they exist
        if self.plot_data_item is not None:
            self.plot_widget.removeItem(self.plot_data_item)
        if self.smoothed_plot_item is not None:
            self.plot_widget.removeItem(self.smoothed_plot_item)
            self.smoothed_plot_item = None

        # Plot log-transformed data (raw) - but only if not showing smoothed only
        if not self.show_only_smoothed:
            # Plot raw data (semi-transparent if smoothed is shown)
            if self.show_smoothed:
                # Make raw data more transparent when smoothed line is shown
                raw_color = pg.mkColor(line_color)
                raw_color.setAlpha(80)  # Semi-transparent
                pen = pg.mkPen(color=raw_color, width=1)
            else:
                pen = pg.mkPen(color=line_color, width=2)

            self.plot_data_item = self.plot_widget.plot(
                times_filtered,
                data_log,
                pen=pen
            )

        # Add smoothed trend line if enabled
        if self.show_smoothed and len(data_log) > 10:
            # Adaptive window size based on data points (roughly 60 samples = 1 minute at 1Hz)
            # Use larger window for longer time ranges
            window_size = min(max(61, len(data_log) // 50), 301)
            # Ensure window size is odd for symmetric filtering
            if window_size % 2 == 0:
                window_size += 1

            # Apply uniform (moving average) filter for smoothing
            data_smoothed = uniform_filter1d(data_log, size=window_size, mode='nearest')

            # Plot smoothed line with brighter color and thicker line
            smooth_pen = pg.mkPen(color=line_color, width=3)
            self.smoothed_plot_item = self.plot_widget.plot(
                times_filtered,
                data_smoothed,
                pen=smooth_pen
            )

    def change_graph_type(self, graph_type):
        self.current_graph = graph_type
        if graph_type == "foreline":
            self.foreline_radio.setChecked(True)
            self.uhv_radio.setChecked(False)
        else:
            self.foreline_radio.setChecked(False)
            self.uhv_radio.setChecked(True)
        self.update_chart()

    def change_time_window(self, hours):
        self.time_window_hours = hours
        self.time_3h_btn.setChecked(hours == 3)
        self.time_6h_btn.setChecked(hours == 6)
        self.time_12h_btn.setChecked(hours == 12)
        # Update custom input to reflect the preset selection
        self.custom_hours_input.blockSignals(True)
        self.custom_hours_input.setValue(hours)
        self.custom_hours_input.blockSignals(False)
        self.update_chart()

    def on_custom_hours_changed(self, value):
        """Handle custom time window input changes."""
        self.time_window_hours = value
        # Uncheck all preset buttons when custom value is changed
        self.time_3h_btn.setChecked(False)
        self.time_6h_btn.setChecked(False)
        self.time_12h_btn.setChecked(False)
        self.update_chart()

    def toggle_smoothed_line(self, state):
        self.show_smoothed = state == Qt.Checked
        # If unchecking smoothed trend, also uncheck smoothed only
        if not self.show_smoothed:
            self.smooth_only_checkbox.blockSignals(True)
            self.smooth_only_checkbox.setChecked(False)
            self.smooth_only_checkbox.blockSignals(False)
            self.show_only_smoothed = False
        self.update_chart()

    def toggle_smoothed_only(self, state):
        """Toggle showing only the smoothed line (hide raw data)."""
        self.show_only_smoothed = state == Qt.Checked
        # When enabling smoothed only, automatically enable smoothed trend
        if self.show_only_smoothed:
            self.smooth_checkbox.blockSignals(True)
            self.smooth_checkbox.setChecked(True)
            self.smooth_checkbox.blockSignals(False)
            self.show_smoothed = True
        self.update_chart()

    def reset_view(self):
        self.time_data.clear()
        self.foreline_data.clear()
        self.uhv_data.clear()
        self.start_time = datetime.now()
        self.update_chart()

    def send_command(self, cmd):
        self.serial.send_command(cmd)

    def send_command_with_feedback(self, cmd, button):
        """Send a command and provide visual feedback on the button."""
        if not self.connected:
            return

        # Store original style
        original_style = button.styleSheet()
        original_text = button.text()

        # Show "sending" state
        button.setText(f"{original_text}...")
        button.setStyleSheet(
            f"background-color: #f59e0b; color: {style.TXT_STRONG}; padding: 10px; "
            f"font-weight: bold; border-radius: 5px; border: 2px solid #fbbf24;"
        )
        button.setEnabled(False)

        # Send the command
        self.serial.send_command(cmd)

        # Restore button after a short delay
        def restore_button():
            button.setText(original_text)
            button.setStyleSheet(original_style)
            button.setEnabled(True)

        QTimer.singleShot(1000, restore_button)

    def open_maintenance_dialog(self):
        if not self.connected:
            return
        if self.maintenance_dialog is None:
            self.maintenance_dialog = MaintenanceModeDialog(
                self,
                send_command=self.send_command,
                is_connected=lambda: self.connected,
            )
        self.maintenance_dialog.show()
        self.maintenance_dialog.raise_()
        self.maintenance_dialog.activateWindow()

    def closeEvent(self, event):
        if self._demo_mode:
            self._stop_demo_mode()
        if self.connected:
            self.serial.close_port()
        if self.maintenance_dialog is not None:
            self.maintenance_dialog.close()
        # Close pressure log files
        self.pressure_logger.close()
        event.accept()

    @staticmethod
    def _button_style():
        return (
            f"background-color: {style.BTN_BG}; color: {style.TXT}; padding: 6px 12px; "
            f"border-radius: 4px; border: 1px solid {style.BTN_BORDER};"
        )

    @staticmethod
    def _toggle_style(selected_color=None):
        active = selected_color or style.BTN_BG_DOWN
        return (
            f"QPushButton {{ background-color: {style.BTN_BG}; color: {style.TXT}; "
            f"padding: 8px 16px; border-radius: 4px; border: 1px solid {style.BTN_BORDER}; }}"
            f"QPushButton:checked {{ background-color: {active}; color: {style.TXT_STRONG}; }}"
        )


def _apply_theme(app, theme):
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(theme.BG))
    palette.setColor(QPalette.WindowText, QColor(theme.TXT))
    palette.setColor(QPalette.Base, QColor(theme.CARD_BG))
    palette.setColor(QPalette.AlternateBase, QColor(theme.CARD_BG))
    palette.setColor(QPalette.Text, QColor(theme.TXT))
    palette.setColor(QPalette.Button, QColor(theme.BTN_BG))
    palette.setColor(QPalette.ButtonText, QColor(theme.TXT))
    palette.setColor(QPalette.Highlight, QColor(theme.GOOD))
    palette.setColor(QPalette.HighlightedText, QColor(theme.TXT_STRONG))
    app.setPalette(palette)


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    _apply_theme(app, theme_mgr.current)
    theme_mgr.themeChanged.connect(lambda theme: _apply_theme(app, theme))

    window = QMainWindow()
    channels = AppChannels()
    page = PressurePage(channels)
    window.setCentralWidget(page)
    window.setWindowTitle("Vacuum System Control")
    window.resize(1400, 800)
    window.show()

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
