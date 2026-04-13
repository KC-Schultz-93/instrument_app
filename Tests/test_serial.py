"""
Simple serial port test to diagnose connection issues
Run this to test if the Arduino is communicating correctly
"""

import serial
import serial.tools.list_ports
import time

def list_ports():
    """List all available COM ports"""
    print("Available COM ports:")
    ports = serial.tools.list_ports.comports()
    for i, port in enumerate(ports):
        print(f"  {i+1}. {port.device} - {port.description}")
    return ports

def test_connection(port_name='COM3', baudrate=115200, timeout=2):
    """Test connection to Arduino"""
    print(f"\n{'='*60}")
    print(f"Testing connection to {port_name} at {baudrate} baud")
    print(f"{'='*60}\n")

    try:
        print(f"Opening port {port_name}...")
        ser = serial.Serial(port_name, baudrate, timeout=timeout)
        print(f"✓ Port opened successfully")
        print(f"  Is open: {ser.is_open}")
        print(f"  Port: {ser.port}")
        print(f"  Baudrate: {ser.baudrate}")

        print(f"\nWaiting for data (10 seconds)...")
        start_time = time.time()
        line_count = 0

        while (time.time() - start_time) < 10:
            if ser.in_waiting > 0:
                try:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        line_count += 1
                        print(f"  [{line_count}] {line}")
                        if line_count >= 5:
                            print("  ... (showing first 5 lines only)")
                            break
                except Exception as e:
                    print(f"  ✗ Error reading line: {e}")
            time.sleep(0.1)

        if line_count == 0:
            print("  ✗ No data received from Arduino")
            print("  Possible issues:")
            print("    - Wrong baudrate (should be 115200)")
            print("    - Arduino not programmed or not running")
            print("    - Wrong COM port")
        else:
            print(f"\n✓ Successfully received {line_count} lines from Arduino")

        ser.close()
        print("\n✓ Port closed successfully")
        return True

    except serial.SerialException as e:
        print(f"✗ Serial Exception: {e}")
        print("\nPossible solutions:")
        print("  - Check if another program is using COM3")
        print("  - Check if Arduino is properly connected")
        print("  - Try unplugging and replugging the Arduino")
        print("  - Check Device Manager for COM port number")
        return False
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("Arduino Connection Diagnostic Tool")
    print("="*60)

    # List all ports
    ports = list_ports()

    # Test COM3
    if not test_connection('COM3', 115200):
        print("\n" + "="*60)
        print("CONNECTION FAILED!")
        print("="*60)

        # Check if COM3 exists
        port_names = [p.device for p in ports]
        if 'COM3' not in port_names:
            print(f"\nWARNING: COM3 not found in available ports!")
            print(f"Available ports: {', '.join(port_names)}")

    print("\n" + "="*60)
    print("Test complete. Press Enter to exit...")
    input()
