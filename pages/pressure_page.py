
"""
Module: instrument_app.pages.pressure_page
Purpose: UI page for pressures/interlocks: port controls, status pills, pump controls,
         and a time-series plot with log-Y and dynamic min↔hr X-axis.

How it fits:
- Depends on: instrument_app.services.serial_manager.SerialManager
              instrument_app.services.data_recorder.DataRecorder
              instrument_app.ui.plots.TimePressureView
              instrument_app.theme.style
- Used by:    MainWindow (as a tab)

Public API:
- class PressureInterlockPage(QWidget)

Signals / Slots:
- Listens: SerialManager.reading, connectedChanged, status
- Emits:   (none) — delegates TX via SerialManager.send_command()

Changelog:
- 2025-08-23 · 0.1.0 · KC · Refactored UI from legacy INT_Readout into modular page.
- 2025-09-10 · 0.1.1 · KC · Refactored to be composed from reusable widgets.
- 2026-09-21 · 0.2.0 · KC · Edited despite CLAUDE.md's "do not touch" note, with
  explicit user go-ahead: pump RUN/STOP and STATUS buttons now actually send
  serial commands (were local no-ops); added a status/log pill so connection
  errors and device log lines are visible; added MAINT-toggle and Clear Fault
  controls.
"""


from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QGridLayout, QVBoxLayout, QHBoxLayout, QComboBox
)
from instrument_app.services.serial_manager import SerialManager
from instrument_app.services.data_recorder import DataRecorder
from instrument_app.theme import style
from instrument_app.ui import (
    PortToolbar, PressureCard, PumpCard, PillLabel, ThemedButton, TimePressureView,
)
from instrument_app.services.parsing import Reading


class PressureInterlockPage(QWidget):
    """Composes pressure cards, pump cards and the time-pressure plot."""

    def __init__(self, serial: SerialManager, recorder: DataRecorder):
        super().__init__()
        self.serial = serial
        self.recorder = recorder
        self._maint_active = False

        grid = QGridLayout(self)
        grid.setContentsMargins(10, 8, 10, 10)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        # --- top toolbar ---------------------------------------------------------
        self.toolbar = PortToolbar()
        self.toolbar.refresh.connect(self._refresh_ports)
        self.toolbar.connect_requested.connect(self._connect)
        self.toolbar.disconnect_requested.connect(self.serial.disconnect)
        self.toolbar.status_requested.connect(lambda: self.serial.send_command("?"))
        grid.addWidget(self.toolbar, 0, 0, 1, 3)

        # --- status/log line -------------------------------------------------
        self.status_lbl = PillLabel("", bg_role=lambda t: t.CARD_BG)
        grid.addWidget(self.status_lbl, 1, 0, 1, 3)

        # --- left column (cards) -------------------------------------------------
        left = QVBoxLayout(); left.setSpacing(8)
        self.card_fore = PressureCard("Foreline Pressure")
        self.card_uhv = PressureCard("UHV Pressure")
        self.card_tg60 = PumpCard("TG60")
        self.card_tg220 = PumpCard("TG220")
        for card in (self.card_tg60, self.card_tg220):
            # The firmware has one system-wide start/stop, not per-pump control -
            # both cards' buttons send the same S/X command.
            card.btn_run.setToolTip("Sends system START (S) - starts both pumps")
            card.btn_stop.setToolTip("Sends system STOP (X) - stops both pumps")
            card.btn_run.clicked.connect(lambda _=False: self.serial.send_command("S"))
            card.btn_stop.clicked.connect(lambda _=False: self.serial.send_command("X"))
        left.addWidget(self.card_fore)
        left.addWidget(self.card_uhv)
        left.addWidget(self.card_tg60)
        left.addWidget(self.card_tg220)
        left.addStretch(1)
        grid.addLayout(left, 2, 0, 3, 1)
        grid.setColumnStretch(0, 1)

        # --- plot ---------------------------------------------------------------
        self.plot = TimePressureView()
        grid.addWidget(self.plot, 2, 1, 2, 2)
        grid.setColumnStretch(1, 6)

        # --- bottom controls -------------------------------------------------
        bottom = QHBoxLayout(); bottom.setSpacing(8)
        self.btn_view_fore = ThemedButton("Foreline", height=34)
        self.btn_view_uhv = ThemedButton("UHV", height=34)
        self.range_cb = QComboBox();
        self.range_cb.addItems(["1 min", "10 min", "1 hour", "6 hours", "24 hours"]);
        self.range_cb.setFixedHeight(34)
        self.btn_reset = ThemedButton("Reset View", height=34)
        self.btn_maint = ThemedButton("Enter MAINT", height=34)
        self.btn_clear_fault = ThemedButton("Clear Fault", height=34)
        self.maint_indicator = PillLabel("MAINT: OFF", bg_role=lambda t: t.GOOD)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_view_fore)
        bottom.addWidget(self.btn_view_uhv)
        bottom.addWidget(self.range_cb)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_reset)
        bottom.addWidget(self.btn_maint)
        bottom.addWidget(self.btn_clear_fault)
        bottom.addWidget(self.maint_indicator)
        grid.addLayout(bottom, 4, 1, 1, 2)

        # --- wiring -------------------------------------------------------------
        self.btn_view_fore.clicked.connect(lambda: self.plot.set_view("Foreline"))
        self.btn_view_uhv.clicked.connect(lambda: self.plot.set_view("UHV"))
        self.range_cb.currentTextChanged.connect(self.plot.set_time_window)
        self.btn_reset.clicked.connect(getattr(self.plot, "reset_view", lambda: None))
        self.btn_maint.clicked.connect(self._toggle_maint)
        self.btn_clear_fault.clicked.connect(lambda: self.serial.send_command("R"))

        self.serial.reading.connect(self._on_reading)
        self.serial.connectedChanged.connect(self._on_connected)
        self.serial.status.connect(self._on_status)
        self._refresh_ports()

        # --- handlers ------------------------------------------------------------
    def _refresh_ports(self) -> None:
        cb = self.toolbar.port_cb
        cb.clear()
        try:
            ports = self.serial.available_ports()
        except Exception:
            ports = []
        for p in ports:
            desc = getattr(p, "description", "")
            dev = getattr(p, "device", str(p))
            cb.addItem(f"{dev}  ({desc})", dev)
        if not ports:
            cb.addItem("No ports found", None)

    def _connect(self, port: str) -> None:
        try:
            self.serial.connect(port)
        except Exception:
            pass

    def _on_connected(self, ok: bool, tip: str) -> None:
        self.toolbar.set_connected(ok, tip)

    def _on_status(self, msg: str) -> None:
        self.status_lbl.setText(msg)
        self.status_lbl.setToolTip(msg)
        lowered = msg.lower()
        is_problem = any(k in lowered for k in ("open error", "tx error", "serial error", "denied"))
        self.status_lbl.set_roles(lambda t: t.BAD if is_problem else t.CARD_BG)

    def _on_reading(self, r: Reading) -> None:
        self.card_uhv.value.setText(f"{r.uhv_torr:.2E}  TORR" if getattr(r, "uhv_torr", None) is not None else "Sensor Off")
        self.card_fore.value.setText(f"{r.fore_torr:.2E}  TORR" if getattr(r, "fore_torr", None) is not None else "Sensor Off")
        self._set_dot(self.card_tg220.dot, getattr(r, "tg220", ""))
        self._set_dot(self.card_tg60.dot, getattr(r, "tg60", ""))
        self._set_maint_state(bool(getattr(r, "maint", False)))
        self.plot.append(r)
        if hasattr(self.recorder, "append"):
            self.recorder.append(r)

    def _toggle_maint(self) -> None:
        # Entering/exiting MAINT both use the same 'M' command: a first 'M' arms
        # it, a second 'M' within 5s enters/exits it (INT_SYS/MaintenanceMode.cpp).
        # Sending two back-to-back always satisfies that window.
        self.serial.send_command("M")
        self.serial.send_command("M")

    def _set_maint_state(self, active: bool) -> None:
        if active == self._maint_active:
            return
        self._maint_active = active
        self.btn_maint.setText("Exit MAINT" if active else "Enter MAINT")
        self.maint_indicator.setText("MAINT: ON" if active else "MAINT: OFF")
        self.maint_indicator.set_roles(lambda t: t.BAD if active else t.GOOD)
        self.btn_clear_fault.setEnabled(not active)

    def _set_dot(self, dot, status: str) -> None:
        s = (status or "").lower()
        if "normal" in s:
            color = style.GOOD
        elif ("fault" in s) or ("alarm" in s):
            color = "#ff4136"
        else:
            color = style.GRAY
        dot.set_color(color)
        dot.setToolTip(status or "Unknown")

