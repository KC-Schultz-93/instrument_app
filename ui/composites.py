"""Composite widgets built from primitives."""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFrame,
    QLabel,
    QSizePolicy,
    QComboBox,
    QGroupBox,
    QFormLayout,
    QDoubleSpinBox,
    QSpinBox,
    QCheckBox,
)

from .mixins import ThemedMixin
from .primitives import ThemedButton, PillLabel, ValueDisplay, IconDot
from instrument_app.theme.themes import Theme


class PressureCard(ThemedMixin, QFrame):
    """Card showing a pressure value."""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedHeight(110)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.caption = QLabel(title)
        self.caption.setAlignment(Qt.AlignCenter)
        self.value = ValueDisplay("--  TORR")
        lay.addWidget(self.caption)
        lay.addWidget(self.value, 1)

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        self.setStyleSheet(
            f"QFrame#card{{background:{t.CARD_BG}; border:1px solid {t.CARD_BORDER}; border-radius:12px;}}"
        )
        self.caption.setStyleSheet("font:11pt 'Segoe UI';")


class PumpCard(ThemedMixin, QFrame):
    """Card containing run/stop controls for a pump."""

    def __init__(self, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedHeight(110)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        head = QHBoxLayout(); head.setSpacing(8)
        self.caption = QLabel(name); self.caption.setAlignment(Qt.AlignCenter)
        self.dot = IconDot()
        head.addWidget(self.caption); head.addStretch(1); head.addWidget(self.dot)

        btnrow = QHBoxLayout(); btnrow.setSpacing(8)
        self.btn_run = ThemedButton("RUN", height=32)
        self.btn_stop = ThemedButton("STOP", height=32)
        btnrow.addWidget(self.btn_run); btnrow.addWidget(self.btn_stop)

        lay.addLayout(head)
        lay.addLayout(btnrow)

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        self.setStyleSheet(
            f"QFrame#card{{background:{t.CARD_BG}; border:1px solid {t.CARD_BORDER}; border-radius:12px;}}"
        )
        self.caption.setStyleSheet("font:11pt 'Segoe UI';")


class PortToolbar(ThemedMixin, QWidget):
    """Top toolbar for serial port selection and connection controls."""

    refresh = pyqtSignal()
    connect_requested = pyqtSignal(str)
    disconnect_requested = pyqtSignal()
    status_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QHBoxLayout(self); lay.setSpacing(8)

        self.port_cb = QComboBox(); self.port_cb.setFixedHeight(36)
        self.btn_refresh = ThemedButton("Refresh", height=36)
        self.btn_connect = ThemedButton("Connect", height=36)
        self.btn_disconnect = ThemedButton("Disconnect", height=36)
        self.btn_status = ThemedButton("STATUS", height=36)
        self.conn = PillLabel("Connection: Not connected", bg_role=lambda t: t.BAD)

        lay.addWidget(self.port_cb, 1)
        lay.addWidget(self.btn_refresh)
        lay.addWidget(self.btn_connect)
        lay.addWidget(self.btn_disconnect)
        lay.addStretch(1)
        lay.addWidget(self.btn_status)
        lay.addWidget(self.conn)

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_connect.clicked.connect(self._emit_connect)
        self.btn_disconnect.clicked.connect(self.disconnect_requested)
        self.btn_status.clicked.connect(self.status_requested)

    def _emit_connect(self) -> None:
        data = self.port_cb.currentData()
        if data is not None:
            self.connect_requested.emit(str(data))

    def set_connected(self, ok: bool, tip: str = "") -> None:
        self.conn.setText("Connection: Connected" if ok else "Connection: Not connected")
        self.conn.set_roles(lambda t: t.GOOD if ok else t.BAD, lambda t: t.TXT)
        self.conn.setToolTip(tip)

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        self.port_cb.setStyleSheet(
            f"QComboBox{{color:{t.TXT}; background:{t.BTN_BG}; border:1px solid {t.BTN_BORDER}; padding:4px 8px; border-radius:8px;}}"
        )


class AODOPanel(ThemedMixin, QWidget):
    """Analog and digital output controls."""

    apply_ao = pyqtSignal(float, float, int)
    write_do = pyqtSignal(str, bool)
    pulse_do = pyqtSignal(str, int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QVBoxLayout(self); lay.setSpacing(10)

        # AO group
        gb_ao = QGroupBox("Electrode Voltages (AO)")
        form = QFormLayout(gb_ao); form.setLabelAlignment(Qt.AlignRight)
        self.sp_ao0 = QDoubleSpinBox(); self._cfg_spin(self.sp_ao0, -10.0, 10.0, 0.01, 0.00, " V")
        self.sp_ao1 = QDoubleSpinBox(); self._cfg_spin(self.sp_ao1, -10.0, 10.0, 0.01, 0.00, " V")
        self.sp_ramp = QSpinBox();      self._cfg_spin(self.sp_ramp, 1, 2000, 1, 100, " ms")
        self.btn_apply = ThemedButton("Apply")
        self.btn_apply.clicked.connect(self._emit_apply)
        form.addRow("AO0 (Endcap A):", self.sp_ao0)
        form.addRow("AO1 (Endcap B):", self.sp_ao1)
        form.addRow("Ramp time:", self.sp_ramp)
        form.addRow("", self.btn_apply)

        # DO group
        gb_do = QGroupBox("Digital Lines (DO)")
        grid = QGridLayout(gb_do)
        self.chk_do0 = QCheckBox("port0/line0")
        self.chk_do1 = QCheckBox("port0/line1")
        self.btn_pulse = ThemedButton("Pulse line0 (50 ms)")
        self.chk_do0.stateChanged.connect(lambda s: self.write_do.emit("port0/line0", s == Qt.Checked))
        self.chk_do1.stateChanged.connect(lambda s: self.write_do.emit("port0/line1", s == Qt.Checked))
        self.btn_pulse.clicked.connect(lambda: self.pulse_do.emit("port0/line0", 50))
        grid.addWidget(self.chk_do0, 0, 0)
        grid.addWidget(self.chk_do1, 0, 1)
        grid.addWidget(self.btn_pulse, 1, 0, 1, 2)

        lay.addWidget(gb_ao)
        lay.addWidget(gb_do)
        lay.addStretch(1)

        self._spin_boxes = [self.sp_ao0, self.sp_ao1, self.sp_ramp]
        self._checkboxes = [self.chk_do0, self.chk_do1]

    def _cfg_spin(self, sp, mn, mx, step, val, suffix):
        if isinstance(sp, QDoubleSpinBox):
            sp.setRange(float(mn), float(mx)); sp.setSingleStep(float(step)); sp.setDecimals(3); sp.setValue(float(val))
        else:
            sp.setRange(int(mn), int(mx)); sp.setSingleStep(int(step)); sp.setValue(int(val))
        sp.setButtonSymbols(QDoubleSpinBox.NoButtons)
        sp.setSuffix(suffix)

    def _emit_apply(self) -> None:
        self.apply_ao.emit(float(self.sp_ao0.value()), float(self.sp_ao1.value()), int(self.sp_ramp.value()))

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        style = (
            f"QDoubleSpinBox,QSpinBox{{color:{t.TXT}; background:{t.BTN_BG}; border:1px solid {t.BTN_BORDER};"
            f"padding:4px 8px; border-radius:8px; font:10pt 'Segoe UI'; min-width:120px;}}"
        )
        for sp in self._spin_boxes:
            sp.setStyleSheet(style)
        chk_ss = f"QCheckBox{{color:{t.TXT}; font:10pt 'Segoe UI';}}"
        for chk in self._checkboxes:
            chk.setStyleSheet(chk_ss)


class AcquisitionPanel(ThemedMixin, QWidget):
    """Acquisition source controls."""

    start_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()
    sourceChanged = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        grid = QGridLayout(self)

        self.cb_source = QComboBox(); self.cb_source.addItems(["Synthetic", "PicoScope (Rapid)", "PicoScope (Streaming)"])
        self.cb_source.currentTextChanged.connect(self.sourceChanged)
        self.chk_synth = QCheckBox("Use synthetic generator"); self.chk_synth.setChecked(True)
        self.sp_fs = QDoubleSpinBox(); self._cfg(self.sp_fs, 100_000, 5_000_000, 1_000, 2_400_000, " Hz")
        self.sp_N = QSpinBox(); self._cfg(self.sp_N, 16_384, 1_048_576, 1024, 262_144, "")
        self.sp_period = QSpinBox(); self._cfg(self.sp_period, 10, 2000, 10, 250, " ms")
        self.btn_start = ThemedButton("Start")
        self.btn_stop = ThemedButton("Stop"); self.btn_stop.setEnabled(False)
        self.lbl_hint = QLabel("")

        self.btn_start.clicked.connect(self.start_clicked)
        self.btn_stop.clicked.connect(self.stop_clicked)

        grid.addWidget(QLabel("Source:"), 0, 0); grid.addWidget(self.cb_source, 0, 1)
        grid.addWidget(self.chk_synth, 1, 0, 1, 2)
        grid.addWidget(QLabel("Sample rate:"), 2, 0); grid.addWidget(self.sp_fs, 2, 1)
        grid.addWidget(QLabel("Samples per event:"), 3, 0); grid.addWidget(self.sp_N, 3, 1)
        grid.addWidget(QLabel("Event period:"), 4, 0); grid.addWidget(self.sp_period, 4, 1)
        grid.addWidget(self.btn_start, 5, 0); grid.addWidget(self.btn_stop, 5, 1)
        grid.addWidget(self.lbl_hint, 6, 0, 1, 2)

    # --- helpers -----------------------------------------------------------------
    def _cfg(self, sp, mn, mx, step, val, suffix):
        if isinstance(sp, QDoubleSpinBox):
            sp.setRange(float(mn), float(mx)); sp.setSingleStep(float(step)); sp.setDecimals(3); sp.setValue(float(val))
        else:
            sp.setRange(int(mn), int(mx)); sp.setSingleStep(int(step)); sp.setValue(int(val))
        sp.setButtonSymbols(QDoubleSpinBox.NoButtons)
        if suffix:
            sp.setSuffix(suffix)

    # --- public API ---------------------------------------------------------------
    def source(self) -> str:
        return self.cb_source.currentText()

    def sample_rate(self) -> float:
        return float(self.sp_fs.value())

    def samples(self) -> int:
        return int(self.sp_N.value())

    def period_ms(self) -> int:
        return int(self.sp_period.value())

    def use_synth(self) -> bool:
        return self.chk_synth.isChecked()

    def set_hint(self, text: str) -> None:
        self.lbl_hint.setText(text)

    def set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    def enable_synth(self, enabled: bool) -> None:
        self.chk_synth.setEnabled(enabled)

    def set_synth_checked(self, checked: bool) -> None:
        self.chk_synth.setChecked(checked)

    def set_start_enabled(self, enabled: bool) -> None:
        self.btn_start.setEnabled(enabled)

    def apply_theme(self, t: Theme) -> None:  # pragma: no cover - trivial
        style = (
            f"QDoubleSpinBox,QSpinBox{{color:{t.TXT}; background:{t.BTN_BG}; border:1px solid {t.BTN_BORDER};"
            f"padding:4px 8px; border-radius:8px; font:10pt 'Segoe UI'; min-width:120px;}}"
        )
        for sp in (self.sp_fs, self.sp_N, self.sp_period):
            sp.setStyleSheet(style)
        self.chk_synth.setStyleSheet(f"QCheckBox{{color:{t.TXT}; font:10pt 'Segoe UI';}}")
        self.cb_source.setStyleSheet(
            f"QComboBox{{color:{t.TXT}; background:{t.BTN_BG}; border:1px solid {t.BTN_BORDER}; padding:4px 8px; border-radius:8px;}}"
        )