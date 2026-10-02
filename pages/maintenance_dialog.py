"""
Module: instrument_app.pages.maintenance_dialog
Purpose: Modeless dialog for manual relay control while INT_SYS is in MAINT
         mode (interlocks/sequencing bypassed - see INT_SYS/MaintenanceMode.cpp,
         INT_SYS.ino's MAINT-active command switch).

How it fits:
- Depends on: instrument_app.services.serial_manager.SerialManager,
              instrument_app.services.parsing.Reading, instrument_app.ui
- Used by:    PressureInterlockPage (opened from the MAINT button)

Firmware commands used here (single ASCII byte, see
INT_SYS/SerialInterface.cpp readSerialCmd()): 6/7 = TG60 on/off, 2/3 = TG220
on/off, H/J = Hornet on/off (NOTE: 'H' means something different in normal
mode - clears the Hornet fault there - so this dialog must never share a
handler with PressureInterlockPage's Clear Fault button), O/C = Test relay
on/off, 0 = all off, M = exit MAINT (any single M while active exits
unconditionally).

Public API:
- class MaintenanceDialog(QDialog):
    update_from_reading(Reading), set_maint_active(bool)

Changelog:
- 2026-09-30 · 0.1.0 · KC · New file, added despite CLAUDE.md's "do not touch"
  note on pressure-related functionality, with explicit user go-ahead.
"""

from __future__ import annotations

from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel

from instrument_app.services.serial_manager import SerialManager
from instrument_app.services.parsing import Reading
from instrument_app.theme import style
from instrument_app.ui import ThemedButton, PillLabel, IconDot

# name -> (on_cmd, off_cmd, Reading attribute holding live relay state)
_RELAYS = [
    ("TG60", "6", "7", "rel_tg60"),
    ("TG220", "2", "3", "rel_tg220"),
    ("Hornet", "H", "J", "rel_hornet"),
    ("Test", "O", "C", "rel_test"),
]


class MaintenanceDialog(QDialog):
    """Manual relay control while the Arduino is in MAINT mode."""

    def __init__(self, serial: SerialManager, parent=None):
        super().__init__(parent)
        self.serial = serial
        self.setWindowTitle("Maintenance Mode")
        self.setModal(False)

        v = QVBoxLayout(self)

        self.maint_indicator = PillLabel("MAINT: ON", bg_role=lambda t: t.BAD)
        v.addWidget(self.maint_indicator)

        note = QLabel(
            "Interlocks and automatic pump sequencing are bypassed while in "
            "MAINT mode - relays respond only to the buttons below. The "
            "Arduino automatically exits MAINT (and forces all outputs off) "
            "after 10 minutes with no MAINT command."
        )
        note.setWordWrap(True)
        v.addWidget(note)

        self._rows: dict[str, dict] = {}
        for name, on_cmd, off_cmd, attr in _RELAYS:
            row, dot, btn_on, btn_off = self._build_relay_row(name, on_cmd, off_cmd)
            v.addLayout(row)
            self._rows[attr] = {"dot": dot, "btn_on": btn_on, "btn_off": btn_off}

        self.btn_all_off = ThemedButton("ALL OFF", height=34)
        self.btn_all_off.clicked.connect(lambda: self.serial.send_command("0"))
        v.addWidget(self.btn_all_off)

        self.btn_exit_maint = ThemedButton("Exit MAINT", height=34)
        self.btn_exit_maint.clicked.connect(lambda: self.serial.send_command("M"))
        v.addWidget(self.btn_exit_maint)

    def _build_relay_row(self, name: str, on_cmd: str, off_cmd: str):
        row = QHBoxLayout(); row.setSpacing(8)
        label = QLabel(name)
        label.setFixedWidth(60)
        dot = IconDot()
        btn_on = ThemedButton(f"{name} ON", height=32)
        btn_off = ThemedButton(f"{name} OFF", height=32)
        btn_on.clicked.connect(lambda _=False, c=on_cmd: self.serial.send_command(c))
        btn_off.clicked.connect(lambda _=False, c=off_cmd: self.serial.send_command(c))
        row.addWidget(label)
        row.addWidget(dot)
        row.addWidget(btn_on)
        row.addWidget(btn_off)
        return row, dot, btn_on, btn_off

    def update_from_reading(self, r: Reading) -> None:
        for attr, widgets in self._rows.items():
            on = bool(getattr(r, attr, False))
            widgets["dot"].set_color(style.GOOD if on else style.GRAY)
        self.set_maint_active(bool(getattr(r, "maint", False)))

    def set_maint_active(self, active: bool) -> None:
        self.maint_indicator.setText("MAINT: ON" if active else "MAINT: OFF")
        self.maint_indicator.set_roles(lambda t: t.BAD if active else t.GOOD)
        for widgets in self._rows.values():
            widgets["btn_on"].setEnabled(active)
            widgets["btn_off"].setEnabled(active)
        self.btn_all_off.setEnabled(active)
        self.btn_exit_maint.setEnabled(active)
