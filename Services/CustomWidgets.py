from PyQt5.QtWidgets import QGroupBox, QVBoxLayout, QLabel, QHBoxLayout, QPushButton
from PyQt5.QtCore import pyqtSignal

from UI.theme import style


class QGaugeDisplay(QGroupBox):
    def __init__(self, title, value="0.000", unit=""):
        super().__init__(title)

        layout = QVBoxLayout()

        # Create horizontal layout for value and status indicator
        value_layout = QHBoxLayout()

        self.value_label = QLabel(value)
        self.value_label.setObjectName("value")
        self.value_label.setStyleSheet(
            f"color: {style.TXT_STRONG}; font-size: 28px; font-weight: bold;"
        )
        value_layout.addWidget(self.value_label)

        # Add stretch to push status indicator to the right
        value_layout.addStretch()

        # Status indicator light
        self.status_indicator = QLabel("●")
        self.status_indicator.setStyleSheet(
            f"color: {style.BAD}; font-size: 24px; font-weight: bold;"
        )
        value_layout.addWidget(self.status_indicator)
        layout.addLayout(value_layout)

        self.unit_label = QLabel(unit)
        self.unit_label.setStyleSheet(
            f"color: {style.GRAY}; font-size: 10px;"
        )
        layout.addWidget(self.unit_label)

        self.setLayout(layout)

    def set_value(self, text):
        self.value_label.setText(text)

    def set_unit(self, text):
        self.unit_label.setText(text)

    def set_status(self, energized):
        """Set the status indicator color based on whether the gauge is energized."""
        if energized:
            self.status_indicator.setStyleSheet(
                f"color: {style.GOOD}; font-size: 24px; font-weight: bold;"
            )
        else:
            self.status_indicator.setStyleSheet(
                f"color: {style.BAD}; font-size: 24px; font-weight: bold;"
            )


class QPumpControl(QGroupBox):
    runClicked = pyqtSignal()
    stopClicked = pyqtSignal()

    def __init__(self, title):
        super().__init__(title)

        layout = QVBoxLayout()

        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel(title))
        status_layout.addStretch()

        self.status_label = QLabel("NO")
        self.status_label.setObjectName("status")
        status_layout.addWidget(self.status_label)
        layout.addLayout(status_layout)

        button_layout = QHBoxLayout()
        self.run_btn = QPushButton("RUN")
        self.run_btn.clicked.connect(self.runClicked.emit)
        self.run_btn.setObjectName("run")
        button_layout.addWidget(self.run_btn)

        self.stop_btn = QPushButton("STOP")
        self.stop_btn.clicked.connect(self.stopClicked.emit)
        self.stop_btn.setObjectName("stop")
        button_layout.addWidget(self.stop_btn)
        layout.addLayout(button_layout)

        self.setLayout(layout)
        self.apply_styles()
        self.set_status("NO")

    def apply_styles(self):
        self.run_btn.setStyleSheet(
            f"background-color: {style.GOOD}; color: {style.TXT_STRONG}; "
            "padding: 8px; font-size: 11px; font-weight: bold; border-radius: 3px;"
        )
        self.stop_btn.setStyleSheet(
            f"background-color: {style.BAD}; color: {style.TXT_STRONG}; "
            "padding: 8px; font-size: 11px; font-weight: bold; border-radius: 3px;"
        )

    def set_status(self, status):
        self.status_label.setText(status)
        # Update button texts based on status
        if status.upper() == "OK":
            self.run_btn.setText("RUNNING")
            self.stop_btn.setText("STOPPED")
        else:
            self.run_btn.setText("RUN")
            self.stop_btn.setText("STOP")
        if status == "OK":
            self.status_label.setStyleSheet(
                f"padding: 3px 8px; background-color: {style.GOOD}; "
                f"color: {style.TXT_STRONG}; border-radius: 3px; font-size: 10px;"
            )
        else:
            self.status_label.setStyleSheet(
                f"padding: 3px 8px; background-color: {style.BAD}; "
                f"color: {style.TXT_STRONG}; border-radius: 3px; font-size: 10px;"
            )
