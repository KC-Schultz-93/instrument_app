from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QGroupBox, QLabel, QHBoxLayout,
                             QPushButton, QGridLayout)
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QPalette, QColor


class MaintenanceModeDialog(QDialog):
    def __init__(self, parent=None, send_command=None, is_connected=None):
        super().__init__(parent)
        self._send_command = send_command
        self._is_connected = is_connected

        self.setWindowTitle("Maintenance Mode")
        self.setModal(False)
        self.setGeometry(200, 200, 500, 600)

        self.in_maint_mode = False

        self.init_ui()
        self.apply_dark_theme()

    def init_ui(self):
        layout = QVBoxLayout()

        warning_group = QGroupBox("WARNING")
        warning_layout = QVBoxLayout()
        warning_label = QLabel(
            "MAINTENANCE MODE bypasses all interlocks and safety sequences.\n"
            "Use ONLY for testing and troubleshooting.\n\n"
            "Relays respond only to manual commands\n"
            "Auto-timeout: 10 minutes of inactivity forces outputs OFF"
        )
        warning_label.setStyleSheet("color: #fbbf24; font-size: 11px; padding: 10px;")
        warning_label.setWordWrap(True)
        warning_layout.addWidget(warning_label)
        warning_group.setLayout(warning_layout)
        layout.addWidget(warning_group)

        mode_group = QGroupBox("Maintenance Mode Control")
        mode_layout = QVBoxLayout()

        self.mode_status_label = QLabel("Status: NORMAL MODE")
        self.mode_status_label.setStyleSheet(
            "padding: 8px; background-color: #059669; color: white; "
            "border-radius: 4px; font-weight: bold; font-size: 13px;"
        )
        mode_layout.addWidget(self.mode_status_label)

        mode_btn_layout = QHBoxLayout()

        self.enter_maint_btn = QPushButton("Enter Maintenance Mode")
        self.enter_maint_btn.clicked.connect(self.enter_maintenance_mode)
        self.enter_maint_btn.setStyleSheet(
            "background-color: #d97706; color: white; padding: 10px; "
            "font-weight: bold; border-radius: 5px; font-size: 12px;"
        )
        mode_btn_layout.addWidget(self.enter_maint_btn)

        self.exit_maint_btn = QPushButton("Exit Maintenance Mode")
        self.exit_maint_btn.clicked.connect(self.exit_maintenance_mode)
        self.exit_maint_btn.setEnabled(False)
        self.exit_maint_btn.setStyleSheet(
            "background-color: #dc2626; color: white; padding: 10px; "
            "font-weight: bold; border-radius: 5px; font-size: 12px;"
        )
        mode_btn_layout.addWidget(self.exit_maint_btn)

        mode_layout.addLayout(mode_btn_layout)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)

        relay_group = QGroupBox("Manual Relay Control")
        relay_layout = QGridLayout()

        relay_layout.addWidget(QLabel("TG60:"), 0, 0)
        self.tg60_on_btn = QPushButton("TG60 ON (6)")
        self.tg60_on_btn.clicked.connect(lambda: self.send_maint_command('6'))
        self.tg60_on_btn.setEnabled(False)
        relay_layout.addWidget(self.tg60_on_btn, 0, 1)

        self.tg60_off_btn = QPushButton("TG60 OFF (7)")
        self.tg60_off_btn.clicked.connect(lambda: self.send_maint_command('7'))
        self.tg60_off_btn.setEnabled(False)
        relay_layout.addWidget(self.tg60_off_btn, 0, 2)

        relay_layout.addWidget(QLabel("TG220:"), 1, 0)
        self.tg220_on_btn = QPushButton("TG220 ON (2)")
        self.tg220_on_btn.clicked.connect(lambda: self.send_maint_command('2'))
        self.tg220_on_btn.setEnabled(False)
        relay_layout.addWidget(self.tg220_on_btn, 1, 1)

        self.tg220_off_btn = QPushButton("TG220 OFF (3)")
        self.tg220_off_btn.clicked.connect(lambda: self.send_maint_command('3'))
        self.tg220_off_btn.setEnabled(False)
        relay_layout.addWidget(self.tg220_off_btn, 1, 2)

        relay_layout.addWidget(QLabel("Hornet:"), 2, 0)
        self.hornet_on_btn = QPushButton("Hornet ON (H)")
        self.hornet_on_btn.clicked.connect(lambda: self.send_maint_command('H'))
        self.hornet_on_btn.setEnabled(False)
        relay_layout.addWidget(self.hornet_on_btn, 2, 1)

        self.hornet_off_btn = QPushButton("Hornet OFF (J)")
        self.hornet_off_btn.clicked.connect(lambda: self.send_maint_command('J'))
        self.hornet_off_btn.setEnabled(False)
        relay_layout.addWidget(self.hornet_off_btn, 2, 2)

        relay_layout.addWidget(QLabel("Test Relay:"), 3, 0)
        self.test_on_btn = QPushButton("Test ON (O)")
        self.test_on_btn.clicked.connect(lambda: self.send_maint_command('O'))
        self.test_on_btn.setEnabled(False)
        relay_layout.addWidget(self.test_on_btn, 3, 1)

        self.test_off_btn = QPushButton("Test OFF (C)")
        self.test_off_btn.clicked.connect(lambda: self.send_maint_command('C'))
        self.test_off_btn.setEnabled(False)
        relay_layout.addWidget(self.test_off_btn, 3, 2)

        relay_btn_style = (
            "background-color: #334155; color: white; padding: 8px; "
            "border-radius: 4px; font-size: 11px;"
        )
        for btn in [
            self.tg60_on_btn, self.tg60_off_btn, self.tg220_on_btn, self.tg220_off_btn,
            self.hornet_on_btn, self.hornet_off_btn, self.test_on_btn, self.test_off_btn
        ]:
            btn.setStyleSheet(relay_btn_style)

        relay_group.setLayout(relay_layout)
        layout.addWidget(relay_group)

        self.all_off_btn = QPushButton("ALL RELAYS OFF (0)")
        self.all_off_btn.clicked.connect(lambda: self.send_maint_command('0'))
        self.all_off_btn.setEnabled(False)
        self.all_off_btn.setStyleSheet(
            "background-color: #7c2d12; color: white; padding: 12px; "
            "font-weight: bold; border-radius: 5px; font-size: 13px;"
        )
        layout.addWidget(self.all_off_btn)

        instructions_group = QGroupBox("Command Reference")
        instructions_layout = QVBoxLayout()
        instructions_text = QLabel(
            "To enter MAINT mode, send 'M' twice within 5 seconds.\n"
            "Relay commands:\n"
            "  6 = TG60 ON    | 7 = TG60 OFF\n"
            "  2 = TG220 ON   | 3 = TG220 OFF\n"
            "  H = Hornet ON  | J = Hornet OFF\n"
            "  O = Test ON    | C = Test OFF\n"
            "  0 = All OFF    | M = Exit MAINT"
        )
        instructions_text.setStyleSheet(
            "color: #94a3b8; font-size: 10px; font-family: monospace;"
        )
        instructions_layout.addWidget(instructions_text)
        instructions_group.setLayout(instructions_layout)
        layout.addWidget(instructions_group)

        layout.addStretch()
        self.setLayout(layout)

    def apply_dark_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(15, 23, 42))
        palette.setColor(QPalette.WindowText, QColor(241, 245, 249))
        palette.setColor(QPalette.Base, QColor(30, 41, 59))
        palette.setColor(QPalette.AlternateBase, QColor(51, 65, 85))
        palette.setColor(QPalette.Text, QColor(241, 245, 249))
        palette.setColor(QPalette.Button, QColor(51, 65, 85))
        palette.setColor(QPalette.ButtonText, QColor(241, 245, 249))
        self.setPalette(palette)

    def _can_send(self):
        return callable(self._is_connected) and self._is_connected()

    def _send(self, cmd):
        if callable(self._send_command):
            self._send_command(cmd)

    def enter_maintenance_mode(self):
        if self._can_send():
            self._send('M')
            QTimer.singleShot(100, lambda: self._send('M'))
            QTimer.singleShot(500, self.enable_maint_controls)

    def exit_maintenance_mode(self):
        if self._can_send():
            self._send('M')
            self.disable_maint_controls()

    def enable_maint_controls(self):
        # Only update if state actually changed to prevent redundant updates
        if self.in_maint_mode:
            return

        self.in_maint_mode = True
        self.mode_status_label.setText("Status: MAINTENANCE MODE ACTIVE")
        self.mode_status_label.setStyleSheet(
            "padding: 8px; background-color: #dc2626; color: white; "
            "border-radius: 4px; font-weight: bold; font-size: 13px;"
        )

        self.enter_maint_btn.setEnabled(False)
        self.exit_maint_btn.setEnabled(True)

        self.tg60_on_btn.setEnabled(True)
        self.tg60_off_btn.setEnabled(True)
        self.tg220_on_btn.setEnabled(True)
        self.tg220_off_btn.setEnabled(True)
        self.hornet_on_btn.setEnabled(True)
        self.hornet_off_btn.setEnabled(True)
        self.test_on_btn.setEnabled(True)
        self.test_off_btn.setEnabled(True)
        self.all_off_btn.setEnabled(True)

    def disable_maint_controls(self):
        # Only update if state actually changed to prevent redundant updates
        if not self.in_maint_mode:
            return

        self.in_maint_mode = False
        self.mode_status_label.setText("Status: NORMAL MODE")
        self.mode_status_label.setStyleSheet(
            "padding: 8px; background-color: #059669; color: white; "
            "border-radius: 4px; font-weight: bold; font-size: 13px;"
        )

        self.enter_maint_btn.setEnabled(True)
        self.exit_maint_btn.setEnabled(False)

        self.tg60_on_btn.setEnabled(False)
        self.tg60_off_btn.setEnabled(False)
        self.tg220_on_btn.setEnabled(False)
        self.tg220_off_btn.setEnabled(False)
        self.hornet_on_btn.setEnabled(False)
        self.hornet_off_btn.setEnabled(False)
        self.test_on_btn.setEnabled(False)
        self.test_off_btn.setEnabled(False)
        self.all_off_btn.setEnabled(False)

    def send_maint_command(self, cmd):
        if self._can_send() and self.in_maint_mode:
            self._send(cmd)
