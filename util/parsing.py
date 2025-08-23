
"""
Module: instrument_app.util.parsing
Purpose: Parse Arduino CSV-ish lines into a typed Reading dataclass.

How it fits:
- Depends on: dataclasses
- Used by:    SerialWorker (line→Reading), DataRecorder (type hints)

Public API:
- @dataclass Reading(t_s, uhv_torr, fore_torr, tg220, tg60)
- def parse_arduino_line(line: str) -> Optional[Reading]

Changelog:
- 2025-08-23 · 0.1.0 · KC · Robust parser; tolerant to units and missing fields.
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

def _clean_float(token: str) -> Optional[float]:
    token = token.replace(" Torr","").replace(" TORR","").strip()
    try:
        return float(token)
    except Exception:
        return None

def parse_arduino_line(line: str) -> Optional[Reading]:
    s = (line or "").strip()
    if not s or s.startswith("#") or s.lower().startswith("time"):
        return None
    parts = [p.strip() for p in s.split(",")]
    try:
        t = float(parts[0])
        uhv = _clean_float(parts[1])
        fore = _clean_float(parts[2])
        # pump status slots vary by sketch version; handle both
        tg220 = tg60 = ""
        if len(parts) >= 7 and ("TG220" in parts[5] or "TG60" in parts[6]):
            tg220, tg60 = parts[5], parts[6]
        elif len(parts) >= 9 and ("TG220" in parts[7] or "TG60" in parts[8]):
            tg220, tg60 = parts[7], parts[8]
        return Reading(t, uhv, fore, tg220, tg60)
    except Exception:
        return None
