
"""
Module: instrument_app.services.parsing
Purpose: Parse Arduino CSV-ish lines into a typed Reading dataclass.

How it fits:
- Depends on: dataclasses
- Used by:    SerialWorker (line→Reading), PressureLogger (type hints)

Wire format (INT_SYS/SerialInterface.cpp printCsvHeader/printCsvLineAveraged):
    ms,uhv_V,fore_V,uhv_Torr,fore_Torr,state,
    tg60_ok,tg220_ok,rel_tg60,rel_tg220,rel_hornet,rel_test,
    fault_hornet,fault_system,maint

Public API:
- @dataclass Reading(t_s, uhv_torr, fore_torr, tg220, tg60, maint)
- def parse_arduino_line(line: str) -> Optional[Reading]

Changelog:
- 2025-08-23 · 0.1.0 · KC · Robust parser; tolerant to units and missing fields.
- 2026-09-21 · 0.2.0 · KC · Rewritten for the INT_SYS.ino/SerialInterface.cpp wire
  format merged in from nano_daq (uhv_Torr/fore_Torr in columns 3/4, ms timestamp,
  OK/NO pump status in columns 6/7). Old column layout is no longer produced by
  any firmware in this repo.
- 2026-09-21 · 0.3.0 · KC · Added maint field, read from column 14 (maint 1/0),
  so the UI can track/display MAINT-mode state.
"""

from dataclasses import dataclass
from typing import Optional

@dataclass
class Reading:
    t_s: float
    uhv_torr: Optional[float]
    fore_torr: Optional[float]
    tg220: str
    tg60: str
    maint: bool = False

def _clean_float(token: str) -> Optional[float]:
    token = token.replace(" Torr","").replace(" TORR","").strip()
    try:
        return float(token)
    except Exception:
        return None

def parse_arduino_line(line: str) -> Optional[Reading]:
    s = (line or "").strip()
    if not s or s.startswith("#") or s.lower().startswith("time") or s.lower().startswith("ms,"):
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 8:
        return None
    try:
        t = float(parts[0]) / 1000.0  # ms -> s
        uhv = _clean_float(parts[3])   # uhv_Torr
        fore = _clean_float(parts[4])  # fore_Torr
        tg60 = "Normal" if parts[6].strip().upper() == "OK" else "Fault"
        tg220 = "Normal" if parts[7].strip().upper() == "OK" else "Fault"
        maint = parts[14].strip() == "1" if len(parts) > 14 else False
        return Reading(t, uhv, fore, tg220, tg60, maint)
    except Exception:
        return None
