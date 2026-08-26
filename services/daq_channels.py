"""
daq_channels.py
---------------
Qt signal bus for the DAQ page and its acquisition worker.

Kept separate from any serial/pressure signals so the DAQ subsystem stays
decoupled from serial/pressure code, per project architecture rules.
"""
from __future__ import annotations

from PyQt5.QtCore import QObject, pyqtSignal


class DAQChannels(QObject):
    daq_status = pyqtSignal(str)            # human-readable status messages
    daq_error = pyqtSignal(str)             # error string from DAQ subsystem
    daq_connected = pyqtSignal(bool)        # True = connected, False = disconnected
    daq_waveform = pyqtSignal(object)       # emits WaveformRecord
    daq_event_summary = pyqtSignal(object)  # emits EventSummary
    daq_run_started = pyqtSignal(str)       # run_id
    daq_run_stopped = pyqtSignal(str, int)  # run_id, total_traces

    def __init__(self, parent=None):
        super().__init__(parent)
