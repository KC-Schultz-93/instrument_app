
"""
Module: instrument_app.pages.pressure_page
Purpose: UI page for pressures/interlocks: port controls, status pills, pump controls,
         and a time-series plot with log-Y and dynamic min↔hr X-axis.

How it fits:
- Depends on: instrument_app.services.serial_manager.SerialManager
              instrument_app.services.data_recorder.DataRecorder
              instrument_app.widgets.time_pressure_plot.TimePressurePlot
              instrument_app.theme.style
- Used by:    MainWindow (as a tab)

Public API:
- class PressureInterlockPage(QWidget)

Signals / Slots:
- Listens: SerialManager.reading, connectedChanged, status
- Emits:   (none) — delegates TX via SerialManager.send_command()

Changelog:
- 2025-08-23 · 0.1.0 · KC · Refactored UI from legacy INT_Readout into modular page.
"""


from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QComboBox, QFrame, QSizePolicy
)

from instrument_app.widgets.time_pressure_plot import TimePressurePlot
from instrument_app.services.serial_manager import SerialManager
from instrument_app.services.data_recorder import DataRecorder

# theming
from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme
from instrument_app.theme import style  # dynamic proxy


class PressureInterlockPage(QWidget):
    """
    Left column: two pressure cards + two pump cards.
    Top bar: port select + connect buttons + connection pill.
    Right: live pressure plot + bottom controls.
    """
    def __init__(self, serial: SerialManager, recorder: DataRecorder):
        super().__init__()
        self.serial = serial
        self.recorder = recorder

        # lists for theme restyling
        self._buttons: List[QPushButton] = []
        self._cards: List[QFrame] = []
        self._pills: List[QLabel] = []
        self._labels: List[QLabel] = []

        grid = QGridLayout(self)
        grid.setContentsMargins(10,8,10,10)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        # --- top bar ---
        top = QHBoxLayout(); top.setSpacing(8)
        self.port_cb = QComboBox(); self.port_cb.setFixedHeight(36)
        btn_refresh = self._btn("Refresh", 36, self._refresh_ports)
        btn_connect = self._btn("Connect", 36, self._connect)
        btn_disconnect = self._btn("Disconnect", 36, self.serial.disconnect)
        btn_status = self._btn("STATUS", 36, lambda: self.serial.status.emit("STATUS requested"))
        self.conn = self._pill("Connection: Not connected", ok=False, h=36)
        top.addWidget(self.port_cb, 1)
        top.addWidget(btn_refresh); top.addWidget(btn_connect); top.addWidget(btn_disconnect)
        top.addStretch(1); top.addWidget(btn_status); top.addWidget(self.conn)
        grid.addLayout(top, 0, 0, 1, 3)

        # --- left column (cards) ---
        left = QVBoxLayout(); left.setSpacing(8)
        self.card_fore, self.lbl_fore = self._pressure_card("Foreline Pressure")
        self.card_uhv,  self.lbl_uhv  = self._pressure_card("UHV Pressure")
        self.card_tg60, self.dot_tg60, self.btn60_run, self.btn60_stop = self._pump_card("TG60")
        self.card_tg220,self.dot_tg220,self.btn220_run,self.btn220_stop = self._pump_card("TG220")
        for w in (self.btn60_run, self.btn60_stop, self.btn220_run, self.btn220_stop):
            # stubs; hook to serial when ready
            w.clicked.connect(lambda _=False, name=w.text(): self.serial.status.emit(f"Pump: {name}"))
        left.addWidget(self.card_fore); left.addWidget(self.card_uhv)
        left.addWidget(self.card_tg60); left.addWidget(self.card_tg220); left.addStretch(1)
        grid.addLayout(left, 1, 0, 3, 1)
        grid.setColumnStretch(0, 1)

        # --- plot ---
        self.plot = TimePressurePlot()
        grid.addWidget(self.plot, 1, 1, 2, 2)
        grid.setColumnStretch(1, 6)

        # --- bottom controls (under plot) ---
        bottom = QHBoxLayout(); bottom.setSpacing(8)
        self.btn_view_fore = self._btn("Foreline", 34)
        self.btn_view_uhv  = self._btn("UHV", 34)
        self.range_cb = QComboBox(); self.range_cb.addItems(["1 min","10 min","1 hour","6 hours","24 hours"]); self.range_cb.setFixedHeight(34)
        self.btn_reset     = self._btn("Reset View", 34, self.plot.reset_view if hasattr(self.plot, "reset_view") else None)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_view_fore); bottom.addWidget(self.btn_view_uhv); bottom.addWidget(self.range_cb)
        bottom.addStretch(1); bottom.addWidget(self.btn_reset)
        grid.addLayout(bottom, 3, 1, 1, 2)
        grid.setColumnStretch(2, 0)

        # serial signals
        self.serial.reading.connect(self._on_reading)
        self.serial.connectedChanged.connect(self._on_connected)
        self.serial.status.connect(self._on_status)

        # theme
        theme_mgr.themeChanged.connect(self._apply_theme_to_self)
        self._apply_theme_to_self(theme_mgr.current)

        # initial
        self._refresh_ports()

    # -------------------- tiny builders --------------------

    def _btn(self, txt: str, h: int, handler=None) -> QPushButton:
        b = QPushButton(txt); b.setFixedHeight(h)
        if handler: b.clicked.connect(handler)
        self._buttons.append(b)
        return b

    def _pill(self, text: str, ok: bool, h: int) -> QLabel:
        lbl = QLabel(text); lbl.setAlignment(Qt.AlignCenter); lbl.setFixedHeight(h)
        self._pills.append(lbl)
        return lbl

    def _pressure_card(self, title: str) -> tuple[QFrame, QLabel]:
        frame = QFrame(); frame.setObjectName("card"); frame.setFixedHeight(110)
        frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._cards.append(frame)
        v = QVBoxLayout(frame); v.setContentsMargins(8,8,8,8); v.setSpacing(6)
        cap = QLabel(title); cap.setAlignment(Qt.AlignCenter); self._labels.append(cap)
        val = QLabel("--  TORR"); val.setAlignment(Qt.AlignCenter)
        v.addWidget(cap); v.addWidget(val, 1)
        return frame, val

    def _pump_card(self, name: str) -> tuple[QFrame, QLabel, QPushButton, QPushButton]:
        frame = QFrame(); frame.setObjectName("card"); frame.setFixedHeight(110)
        self._cards.append(frame)
        v = QVBoxLayout(frame); v.setContentsMargins(8,8,8,8); v.setSpacing(6)
        head = QHBoxLayout(); head.setSpacing(8)
        cap = QLabel(name); cap.setAlignment(Qt.AlignCenter); self._labels.append(cap)
        dot = QLabel(); dot.setFixedSize(14,14)
        head.addWidget(cap); head.addStretch(1); head.addWidget(dot)
        btnrow = QHBoxLayout(); btnrow.setSpacing(8)
        b_run  = self._btn("RUN", 32)
        b_stop = self._btn("STOP", 32)
        btnrow.addWidget(b_run); btnrow.addWidget(b_stop)
        v.addLayout(head); v.addLayout(btnrow)
        return frame, dot, b_run, b_stop

    # -------------------- theme hook --------------------

    def _apply_theme_to_self(self, t: Theme):
        # cards
        for card in self._cards:
            card.setStyleSheet(f"QFrame#card{{background:{t.CARD_BG}; border:1px solid {t.CARD_BORDER}; border-radius:12px;}}")
        # labels (titles)
        for lab in self._labels:
            lab.setStyleSheet("font:11pt 'Segoe UI';")
        # pressure readouts (second child in pressure cards)
        for frame in (self.card_fore, self.card_uhv):
            val = frame.findChildren(QLabel)[1]
            val.setStyleSheet("font:20pt 'Consolas'; background:#000; color:#ff4136; border-radius:6px; padding:4px;")
        # pump dots default (gray)
        for dot in (self.dot_tg60, self.dot_tg220):
            dot.setStyleSheet("background:#7f8c8d; border-radius:7px; border:1px solid #1b2b34;")
        # buttons
        for b in self._buttons:
            b.setStyleSheet(
                f"QPushButton{{color:{t.TXT}; background:{t.BTN_BG}; border:1px solid {t.BTN_BORDER}; "
                f"padding:6px 10px; border-radius:8px; font:10pt 'Segoe UI';}}"
                f"QPushButton:pressed{{background:{t.BTN_BG_DOWN};}}"
            )
        # pills
        for pill in self._pills:
            ok = "Connected" in pill.text()
            bg = t.GOOD if ok else t.BAD
            pill.setStyleSheet(f"QLabel{{background:{bg}; color:{t.TXT}; padding:6px 10px; border-radius:8px; font:10pt 'Segoe UI';}}")

    # -------------------- serial handlers --------------------

    def _on_reading(self, r):
        # update text
        self.lbl_uhv.setText(f"{r.uhv_torr:.2E}  TORR" if getattr(r, "uhv_torr", None) is not None else "Sensor Off")
        self.lbl_fore.setText(f"{r.fore_torr:.2E}  TORR" if getattr(r, "fore_torr", None) is not None else "Sensor Off")
        # pump dots
        self._set_dot(self.dot_tg220, getattr(r, "tg220", ""))
        self._set_dot(self.dot_tg60,  getattr(r, "tg60",  ""))
        # push to plot/recorder
        if hasattr(self.plot, "append"): self.plot.append(r)
        if hasattr(self.recorder, "append"): self.recorder.append(r)

    def _on_connected(self, ok: bool, tip: str):
        self.conn.setText("Connection: Connected" if ok else "Connection: Not connected")
        self._apply_theme_to_self(theme_mgr.current)
        self.conn.setToolTip(tip or "")

    def _on_status(self, msg: str):
        # hook for toast/log; noop for now
        pass

    # -------------------- helpers --------------------

    def _refresh_ports(self):
        self.port_cb.clear()
        try:
            ports = self.serial.available_ports()
        except Exception:
            ports = []
        for p in ports:
            desc = getattr(p, "description", "")
            dev  = getattr(p, "device", str(p))
            self.port_cb.addItem(f"{dev}  ({desc})", dev)
        if not ports:
            self.port_cb.addItem("No ports found", None)

    def _connect(self):
        data = self.port_cb.currentData()
        if data:
            try: self.serial.connect(data)
            except Exception: pass

    def _set_dot(self, dot: QLabel, status: str):
        s = (status or "").lower()
        if "normal" in s: color = style.GOOD
        elif ("fault" in s) or ("alarm" in s): color = "#ff4136"
        else: color = style.GRAY
        dot.setStyleSheet(f"background:{color}; border-radius:7px; border:1px solid #1b2b34;")
        dot.setToolTip(status or "Unknown")

