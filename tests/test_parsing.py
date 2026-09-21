"""
test_parsing.py
----------------
Unit tests for parse_arduino_line() against the INT_SYS.ino/SerialInterface.cpp
wire format. No hardware required.

Run with:
    python tests/test_parsing.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from instrument_app.services.parsing import parse_arduino_line, Reading

# ms,uhv_V,fore_V,uhv_Torr,fore_Torr,state,tg60_ok,tg220_ok,
# rel_tg60,rel_tg220,rel_hornet,rel_test,fault_hornet,fault_system,maint
_DATA_LINE = "12345,1.2340,0.5670,1.23e-04,5.67e-03,RUN,OK,NO,1,1,1,0,0,0,{maint}"


def test_data_line_maint_on():
    r = parse_arduino_line(_DATA_LINE.format(maint=1))
    assert isinstance(r, Reading)
    assert r.t_s == 12.345, r.t_s
    assert abs(r.uhv_torr - 1.23e-04) < 1e-9, r.uhv_torr
    assert abs(r.fore_torr - 5.67e-03) < 1e-9, r.fore_torr
    assert r.tg60 == "Normal", r.tg60
    assert r.tg220 == "Fault", r.tg220
    assert r.maint is True
    print("  data line, maint=1:  PASS")


def test_data_line_maint_off():
    r = parse_arduino_line(_DATA_LINE.format(maint=0))
    assert r.maint is False
    print("  data line, maint=0:  PASS")


def test_data_line_missing_maint_column():
    # Short line (no maint column at all) - should still parse, defaulting maint
    # to False rather than raising.
    r = parse_arduino_line("12345,1.2340,0.5670,1.23e-04,5.67e-03,RUN,OK,NO")
    assert isinstance(r, Reading)
    assert r.maint is False
    print("  short line (no maint column):  PASS")


def test_firmware_log_line_is_not_data():
    r = parse_arduino_line("RESET denied: Foreline pressure still too high.")
    assert r is None
    print("  firmware log line -> None:  PASS")


def test_csv_header_is_not_data():
    header = ("ms,uhv_V,fore_V,uhv_Torr,fore_Torr,state,"
              "tg60_ok,tg220_ok,rel_tg60,rel_tg220,rel_hornet,rel_test,"
              "fault_hornet,fault_system,maint")
    assert parse_arduino_line(header) is None
    print("  CSV header line -> None:  PASS")


if __name__ == "__main__":
    print("parse_arduino_line tests (no hardware required)")
    print("=" * 60)
    test_data_line_maint_on()
    test_data_line_maint_off()
    test_data_line_missing_maint_column()
    test_firmware_log_line_is_not_data()
    test_csv_header_is_not_data()
    print()
    print("All parsing tests passed.")
