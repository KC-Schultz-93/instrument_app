import serial
import time
from PyQt5.QtCore import QObject, QTimer
import serial.tools.list_ports


class ArduinoSerialComms(QObject):
    def __init__(self, channels=None, baudrate=115200, timeout=0.1, poll_interval_ms=100, parent=None):
        super().__init__(parent)
        self.channels = channels
        self.baudrate = baudrate
        self.timeout = timeout
        self.poll_interval_ms = poll_interval_ms
        self._serial = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._read_serial)

    def refresh_ports(self):
        ports = serial.tools.list_ports.comports()
        return [f"{port.device} ({port.description})" for port in ports]

    def open_port(self, port_name):
        if self._serial:
            return
        try:
            # Add debug logging
            import traceback
            with open('serial_debug.log', 'a') as f:
                f.write(f"Attempting to open {port_name} at {self.baudrate} baud\n")

            # Open serial port with DTR disabled to prevent Arduino reset
            self._serial = serial.Serial()
            self._serial.port = port_name
            self._serial.baudrate = self.baudrate
            self._serial.timeout = self.timeout
            self._serial.dtr = False  # Disable DTR to prevent Arduino reset
            self._serial.open()

            with open('serial_debug.log', 'a') as f:
                f.write(f"Serial port opened successfully (DTR disabled)\n")

            # Wait for Arduino to be ready (in case it did reset)
            time.sleep(2.0)

            # Clear any buffered data from Arduino startup messages
            if self._serial.in_waiting:
                self._serial.read(self._serial.in_waiting)
                with open('serial_debug.log', 'a') as f:
                    f.write(f"Cleared {self._serial.in_waiting} bytes of startup buffer\n")

            self._timer.start(self.poll_interval_ms)
            if self.channels:
                self.channels.connection_changed.emit(True, port_name)

            # Request current state from Arduino after connection
            time.sleep(0.1)
            self._serial.write(b'?')  # Send status query

            with open('serial_debug.log', 'a') as f:
                f.write(f"Connection established, state query sent\n")
        except Exception as exc:
            import traceback
            with open('serial_debug.log', 'a') as f:
                f.write(f"ERROR: {exc}\n")
                f.write(traceback.format_exc())
            if self.channels:
                self.channels.error.emit(f"Serial open failed: {exc}")
            self._serial = None

    def close_port(self):
        if not self._serial:
            return
        self._timer.stop()
        try:
            self._serial.close()
        finally:
            self._serial = None
            if self.channels:
                self.channels.connection_changed.emit(False, "")

    def send_command(self, cmd):
        if not self._serial:
            if self.channels:
                self.channels.error.emit("Not connected to Arduino")
            return
        try:
            self._serial.write(cmd.encode())
            if self.channels:
                self.channels.command_sent.emit(cmd)
        except Exception as exc:
            if self.channels:
                self.channels.error.emit(f"Error sending command: {exc}")

    def _read_serial(self):
        if not self._serial:
            return
        try:
            # Check if port is still open
            if not self._serial.is_open:
                if self.channels:
                    self.channels.error.emit("Serial port closed unexpectedly")
                self.close_port()
                return

            if not self._serial.in_waiting:
                return

            line = self._serial.readline().decode('utf-8', errors='ignore').strip()
        except (OSError, IOError) as exc:
            # Port disconnected or hardware error
            if self.channels:
                self.channels.error.emit(f"Serial port error: {exc}")
            self.close_port()
            return
        except Exception as exc:
            if self.channels:
                self.channels.error.emit(f"Serial read error: {exc}")
            return

        # Debug: log all received lines
        with open('serial_debug.log', 'a') as f:
            f.write(f"RAW: {line}\n")

        if not line or line.startswith('ms,') or line.startswith('===') or line.startswith('Commands'):
            return

        # Also skip other informational lines
        if line.startswith('MAINT') or line.startswith('Entered') or line.startswith('Exited'):
            return
        if line.startswith('Current') or line.startswith('Relays:') or line.startswith('Voltage'):
            return
        if line.startswith('ADC') or line.startswith('==='):
            return

        parsed = self._parse_line(line)

        # Debug: log parsing result
        with open('serial_debug.log', 'a') as f:
            f.write(f"PARSED: {parsed}\n")

        if parsed and self.channels:
            self.channels.data_received.emit(parsed)

    @staticmethod
    def _parse_line(line):
        parts = line.split(',')
        # Support both old (14 columns) and new (15 columns) CSV format
        if len(parts) < 14:
            return None
        try:
            if len(parts) >= 15:
                # New format: fault_hornet, fault_system, maint
                return {
                    "timestamp_ms": int(parts[0]),
                    "uhv_torr": float(parts[3]),
                    "fore_torr": float(parts[4]),
                    "state": parts[5],
                    "tg60_ok": parts[6],
                    "tg220_ok": parts[7],
                    "rel_tg60": int(parts[8]),
                    "rel_tg220": int(parts[9]),
                    "rel_hornet": int(parts[10]),
                    "rel_test": int(parts[11]),
                    "fault_hornet": int(parts[12]),
                    "fault_system": int(parts[13]),
                    "maint": int(parts[14]),
                }
            else:
                # Old format: single fault column
                return {
                    "timestamp_ms": int(parts[0]),
                    "uhv_torr": float(parts[3]),
                    "fore_torr": float(parts[4]),
                    "state": parts[5],
                    "tg60_ok": parts[6],
                    "tg220_ok": parts[7],
                    "rel_tg60": int(parts[8]),
                    "rel_tg220": int(parts[9]),
                    "rel_hornet": int(parts[10]),
                    "rel_test": int(parts[11]),
                    "fault_hornet": int(parts[12]),  # Map old fault to hornet fault
                    "fault_system": int(parts[12]),  # Map old fault to system fault too
                    "maint": int(parts[13]),
                }
        except (ValueError, IndexError):
            return None


class SerialComms():
    def __init__(self, instrument="Compact", port = 'COM3', baudrate=115200, timeout=10):

        self.instrument = instrument
        self.ser = serial.Serial(port = port, baudrate=baudrate, timeout=timeout)
    
    def close(self):
        self.ser.close()

    def getMessageCompact(self, message):
        message_bytes = bytes(message, 'ascii')
        checksum = str(hex(self.crc16(message_bytes, 0, len(message_bytes)))).upper()
        message = message + "@" + checksum[2:] + "\r"
        return message

    def sendCompact(self, message):
        message_bytes = bytes(message, 'ascii')
        message_list = message[0:-6].strip('\r').split(';')
        checksum = str(hex(self.crc16(message_bytes, 0, len(message_bytes)))).upper()
        message = message + "@" + checksum[2:] + "\r"
        message_bytes = bytes(message, 'ascii')
        self.ser.write(message_bytes)
        time.sleep(.015)
        if self.ser.in_waiting > 0:
            data = self.ser.read(self.ser.in_waiting)
            data = data.replace(b'\x00',b'').replace(b'\x06', b'').strip(b'\r').split(b'\r')
            data = [d.decode('ascii') for d in data]
            results = []
            responses = []
            for m, d in zip(message_list, data):
                if m in d:
                    response, checksum = d.split("@")
                    checkchecksum = str(hex(self.crc16(bytes(response, 'ascii'), 0, len(response))))[2:].upper()
                    while len(checkchecksum) < 4:
                        checkchecksum = "0" + checkchecksum
                    if checksum == checkchecksum:
                        if "?" in response:
                            results.append(response.split("?")[-1])
                            responses.append(response)
                        elif "=" in response:
                            results.append(response.split("=")[-1])
                            responses.append(response)
                    else:
                        print("Checksum wrong")
                        return None
        else:
            print("No response to command")
            return None
        if len(results) > 0:
            return results, responses
        else:
            print("Response empty")
            return None, None
        
    @staticmethod
    def crc16(data : bytearray, offset , length):
        if data is None or offset < 0 or offset > len(data)- 1 and offset+length > len(data):
            return 0
        crc = 0xFFFF
        for i in range(0, length):
            crc ^= data[offset + i] << 8
            for j in range(0,8):
                if (crc & 0x8000) > 0:
                    crc =(crc << 1) ^ 0x1021
                else:
                    crc = crc << 1
        return crc & 0xFFFF

if __name__ == '__main__':
    ser = SerialComms()
    #message = ser.getMessageCompact('QP_1:RFA_?')
    message = ser.getMessageCompact('TP_1:MOSW?;TP_1:ROTR?;TP_1:POWR?')
    print(ser.sendCompact(message))

    
    ser.close()
