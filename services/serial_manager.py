
"""
Module: instrument_app.services.serial_manager
Purpose: Threaded serial I/O for the Arduino. Periodically reads lines, parses them,
         emits structured readings, and provides a thread-safe send_command().

How it fits:
- Depends on: pyserial, PyQt (QThread/QTimer), instrument_app.services.parsing
- Used by:    PressureInterlockPage (subscribe to signals), MainWindow (lifecycle)

Public API:
- class SerialManager(QObject): connect(port), disconnect(), send_command(str)
- Signals: reading(Reading), connectedChanged(bool, str), status(str)

Threading model:
- Worker (SerialWorker) lives in a QThread; GUI never blocks on I/O.

Changelog:
- 2025-08-23 · 0.1.0 · KC · Added write_line/send_command and signal wiring.
- 2026-09-21 · 0.2.0 · KC · Edited despite CLAUDE.md's "do not touch" note, with
  explicit user go-ahead: connectedChanged now only fires True after the port
  actually opens (was firing optimistically before open was attempted), and
  non-CSV serial lines (firmware log/refusal messages) are now surfaced via
  status instead of silently dropped. Read period/timeout now match the
  firmware's real print cadence (config/settings.py).
- 2026-09-21 · 0.2.1 · KC · Fixed the actual "connected but zero data, ever" bug:
  the poll QTimer was created in SerialWorker.__init__ (GUI thread, before
  moveToThread), so it never had the worker thread's affinity and silently
  never fired once started there. Timer is now created lazily in start(),
  which runs on the worker thread.
"""


from PyQt5.QtCore import QObject, pyqtSignal, QThread, QTimer
from serial.tools import list_ports
import serial
from time import sleep
from instrument_app.services.parsing import parse_arduino_line, Reading
from instrument_app.config.settings import BAUD_RATE, READ_PERIOD_MS, SERIAL_READ_TIMEOUT_S

class SerialWorker(QObject):
    reading = pyqtSignal(object)  # Reading
    status  = pyqtSignal(str)
    opened  = pyqtSignal(bool, str)  # True+port on success, False+error text on failure

    def __init__(self, port, baud):
        super().__init__()
        self._port, self._baud = port, baud
        self._ser = None
        # QTimer must be created on the thread it will run on. This worker is
        # moveToThread()'d after construction, so the timer can't be built here
        # (it would keep GUI-thread affinity and silently never fire) - it's
        # created lazily in start(), which runs on the worker thread.
        self._timer = None

    def _ensure_timer(self):
        if self._timer is None:
            self._timer = QTimer()
            self._timer.timeout.connect(self._poll_once)

    def write_line(self, line: str):
        try:
            if not self._ser:
                self.status.emit("TX ignored: not connected")
                return
            msg = (line.strip().upper() + "\n").encode("ascii", errors="ignore")
            self._ser.write(msg)
            self._ser.flush()
            self.status.emit(f">> {line.strip().upper()}")
        except Exception as e:
            self.status.emit(f"TX error: {e}")

    def start(self):
        try:
            self._ser = serial.Serial(self._port, self._baud, timeout=SERIAL_READ_TIMEOUT_S)
            sleep(1.2)
            self._ser.reset_input_buffer()
            self.status.emit(f"Connected {self._port}")
            self._ensure_timer()
            self._timer.start(READ_PERIOD_MS)
            self.opened.emit(True, self._port)
        except Exception as e:
            self.status.emit(f"Open error: {e}")
            self.opened.emit(False, str(e))

    def stop(self):
        if self._timer is not None:
            self._timer.stop()
        try:
            if self._ser: 
               self._ser.close()
        finally:
            self._ser = None
            self.status.emit("Disconnected")

    def _poll_once(self):
        if not self._ser:
            return
        try:
            raw = self._ser.readline().decode(errors="replace")
            stripped = raw.strip()
            if not stripped:
                return  # read timed out with nothing waiting; not an error
            r = parse_arduino_line(raw)
            if r:
                self.reading.emit(r)
            else:
                # Not a data line - a firmware banner/log line (e.g. a fault-clear
                # refusal). Surface it instead of silently dropping it.
                self.status.emit(f"DEVICE: {stripped}")
        except Exception as e:
            self.status.emit(f"Serial error: {e}")

class SerialManager(QObject):
    connectedChanged = pyqtSignal(bool, str)
    reading = pyqtSignal(object)
    status  = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._thread = None
        self._worker = None

    @staticmethod
    def available_ports():
        return list(list_ports.comports())

    def connect(self, port: str):
        self.disconnect()
        self._thread = QThread()
        self._worker = SerialWorker(port, BAUD_RATE)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._worker.reading.connect(self.reading)
        self._worker.status.connect(self.status)
        self._worker.opened.connect(self._on_worker_opened)
        self._thread.start()

    def _on_worker_opened(self, ok: bool, info: str) -> None:
        if not ok:
            self._teardown()
        self.connectedChanged.emit(ok, info)

    def send_command(self, cmd: str):
        if self._worker and self._thread and self._thread.isRunning():
            self._worker.write_line(cmd)
        else:
            self.status.emit("TX ignored: not connected")

    def _teardown(self) -> None:
        if self._worker:
            self._worker.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None

    def disconnect(self):
        had_connection = self._worker is not None
        self._teardown()
        if had_connection:
            self.connectedChanged.emit(False, "Disconnected")
