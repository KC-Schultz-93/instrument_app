"""
test_pico_connection.py
-----------------------
Hardware integration test for PicoScope 4262 connection and block acquisition.

REQUIRES:
  - PicoScope 4262 physically connected via USB
  - PicoSDK / PicoScope 6 software installed (provides ps4000.dll on Windows)
  - picosdk-python-wrappers installed:  pip install picosdk-python-wrappers

Run with:
    python Tests/test_pico_connection.py
"""

import sys
from pathlib import Path

# Add repo root to path so Services/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from Services.DAQModels import AcquisitionConfig
from Services.PicoScopeService import PicoScopeService


def test_connect_disconnect():
    print("--- test_connect_disconnect ---")
    svc = PicoScopeService()
    assert not svc.is_connected

    svc.connect()
    assert svc.is_connected
    print("  Connected:  PASS")

    svc.disconnect()
    assert not svc.is_connected
    print("  Disconnected:  PASS")


def test_timebase_resolution():
    print("--- test_timebase_resolution ---")
    svc = PicoScopeService()

    config = AcquisitionConfig(
        channel="A",
        voltage_range_v=2.0,
        coupling="DC",
        sample_interval_ns=200,
        num_samples=5000,
    )
    idx, actual_ns = svc.get_timebase(config)
    print(f"  Requested: 200 ns  →  index={idx}, actual={actual_ns} ns")
    assert actual_ns > 0
    assert abs(actual_ns - 200) < 100, f"Timebase too far off: {actual_ns} ns"
    print("  Timebase resolution:  PASS")


def test_single_block_acquisition():
    print("--- test_single_block_acquisition ---")
    svc = PicoScopeService()

    config = AcquisitionConfig(
        channel="A",
        voltage_range_v=2.0,
        coupling="DC",
        sample_interval_ns=200,   # 200 ns ≈ 5 MS/s
        num_samples=5000,
    )

    svc.connect()
    svc.configure_channel(config)
    svc.set_trigger(config)

    record = svc.run_block(config)

    print(f"  Trace shape: {record.voltage.shape}")
    print(f"  Sample interval: {record.sample_interval_ns} ns")
    print(f"  Voltage min: {record.voltage.min():.4f} V")
    print(f"  Voltage max: {record.voltage.max():.4f} V")
    print(f"  Timestamp: {record.timestamp}")
    print(f"  Overflow: {record.metadata.get('overflow', '?')}")

    assert record.voltage.shape == (5000,), f"Unexpected shape: {record.voltage.shape}"
    assert record.time_ns.shape == (5000,)
    assert abs(record.voltage).max() <= 2.0 * 1.05, "Voltage exceeds range"

    svc.disconnect()
    print("  Single block acquisition:  PASS")


def test_multiple_blocks():
    print("--- test_multiple_blocks ---")
    svc = PicoScopeService()

    config = AcquisitionConfig(
        channel="A",
        voltage_range_v=2.0,
        coupling="DC",
        sample_interval_ns=1000,   # 1 µs = 1 MS/s
        num_samples=1000,
    )

    svc.connect()
    svc.configure_channel(config)
    svc.set_trigger(config)

    for i in range(5):
        record = svc.run_block(config)
        print(f"  Trace {i+1}: shape={record.voltage.shape}, "
              f"max={record.voltage.max():.4f} V")
        assert record.voltage.shape == (1000,)

    svc.disconnect()
    print("  Multiple blocks:  PASS")


if __name__ == "__main__":
    print("PicoScope 4262 hardware connection tests")
    print("=" * 50)
    try:
        test_connect_disconnect()
        test_timebase_resolution()
        test_single_block_acquisition()
        test_multiple_blocks()
        print()
        print("All hardware tests passed.")
    except Exception as exc:
        print(f"\nFAILED: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
