"""
test_pressure_logger.py
------------------------
Unit tests for PressureLogger: daily rotation, per-channel throttling, None
handling, and history read-back. No hardware required.

Run with:
    python tests/test_pressure_logger.py
"""

import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from instrument_app.services.pressure_logger import PressureLogger


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="pressure_logger_test_"))


def test_basic_write_and_files_created():
    pl = PressureLogger(base_dir=_tmp_dir())
    pl.log_pressure(0.0, 1.23e-5, 7.6e-2)
    uhv_files = list(pl.base_dir.rglob("*_UHV_Pressure.csv"))
    fore_files = list(pl.base_dir.rglob("*_Foreline_Pressure.csv"))
    assert len(uhv_files) == 1, uhv_files
    assert len(fore_files) == 1, fore_files
    header = uhv_files[0].read_text().splitlines()[0]
    assert header == "timestamp,elapsed_minutes,uhv_torr", header
    pl.close()
    print("  basic write creates both daily files:  PASS")


def test_none_values_do_not_crash():
    pl = PressureLogger(base_dir=_tmp_dir())
    pl.log_pressure(0.0, None, 7.6e-2)
    pl.log_pressure(0.1, 1.2e-5, None)
    pl.log_pressure(0.2, None, None)
    pl.close()
    print("  None uhv/fore values handled without crashing:  PASS")


def test_foreline_throttled_uhv_not():
    pl = PressureLogger(base_dir=_tmp_dir())
    for i in range(5):
        pl.log_pressure(i * 0.01, 1e-5, 1e-2)
    pl.close()

    uhv_file = next(pl.base_dir.rglob("*_UHV_Pressure.csv"))
    fore_file = next(pl.base_dir.rglob("*_Foreline_Pressure.csv"))
    uhv_rows = len(uhv_file.read_text().splitlines()) - 1
    fore_rows = len(fore_file.read_text().splitlines()) - 1
    assert uhv_rows == 5, uhv_rows
    assert fore_rows == 1, fore_rows  # all 5 calls happen well within 10s
    print("  UHV logs every call, Foreline throttled to one row:  PASS")


def test_rotation_boundary():
    pl = PressureLogger(base_dir=_tmp_dir())
    before_3am = datetime(2026, 1, 5, 2, 0, 0)
    after_3am = datetime(2026, 1, 5, 3, 0, 0)
    assert pl._get_log_date(before_3am) == (before_3am - timedelta(days=1)).date()
    assert pl._get_log_date(after_3am) == after_3am.date()
    pl.close()
    print("  3 AM rotation boundary:  PASS")


def test_read_range_round_trip():
    tmp = _tmp_dir()
    pl = PressureLogger(base_dir=tmp)
    now = time.time()
    pl.log_pressure(0.0, 1.0e-5, 2.0e-2)
    time.sleep(0.05)
    pl.log_pressure(0.01, 2.0e-5, 2.1e-2)
    pl.close()

    pl2 = PressureLogger(base_dir=tmp)
    times, values = pl2.read_range("uhv", now - 5, now + 5)
    assert len(times) == 2, times
    assert abs(values[0] - 1.0e-5) < 1e-9 and abs(values[1] - 2.0e-5) < 1e-9, values
    # Foreline was throttled to one row - only one should come back too.
    fore_times, fore_values = pl2.read_range("foreline", now - 5, now + 5)
    assert len(fore_times) == 1, fore_times
    pl2.close()
    print("  read_range round-trips written data:  PASS")


def test_read_range_empty_when_no_files():
    pl = PressureLogger(base_dir=_tmp_dir())
    times, values = pl.read_range("uhv", time.time() - 3600, time.time())
    assert times == [] and values == []
    pl.close()
    print("  read_range returns empty lists when nothing is logged:  PASS")


if __name__ == "__main__":
    print("PressureLogger tests (no hardware required)")
    print("=" * 60)
    test_basic_write_and_files_created()
    test_none_values_do_not_crash()
    test_foreline_throttled_uhv_not()
    test_rotation_boundary()
    test_read_range_round_trip()
    test_read_range_empty_when_no_files()
    print()
    print("All PressureLogger tests passed.")
