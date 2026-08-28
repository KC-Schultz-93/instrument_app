"""
ratemeter_logger.py
-------------------
Append-only CSV logger for ratemeter peak events.

One instance per recording session. Created by RatemeterPage when the user
clicks "Record Data"; closed when they stop recording or stop acquisition.

Output directory:
    Recorded Data/Ratemeter/
        <run_id>_events.csv

CSV columns (one row per detected peak event):
    timestamp, band, amplitude_mv, width_ns, event_type,
    transit_time_us, velocity_m_s
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from instrument_app.services.daq_models import RatemeterEvent


_CSV_FIELDS = [
    "timestamp",
    "band",
    "amplitude_mv",
    "width_ns",
    "event_type",
    "transit_time_us",
    "velocity_m_s",
]


class RatemeterLogger:
    """
    File I/O manager for one ratemeter recording session.

    Parameters
    ----------
    base_dir : Path
        Root output directory (see default_base_dir()).
    run_id : str
        Unique run identifier stamped on the filename.
    """

    def __init__(self, base_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        self._path = base_dir / f"{run_id}_events.csv"
        base_dir.mkdir(parents=True, exist_ok=True)

        self._file = open(self._path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=_CSV_FIELDS)
        self._writer.writeheader()
        self._file.flush()

    @staticmethod
    def make_run_id() -> str:
        """Generate a run ID from the current datetime: YYYY_MM_DD_HHmmss."""
        return datetime.now().strftime("%Y_%m_%d_%H%M%S")

    @staticmethod
    def default_base_dir() -> Path:
        """Standard output root, parallel to DAQ's Recorded Data/DAQ/."""
        return Path(__file__).parent.parent / "Recorded Data" / "Ratemeter"

    @property
    def path(self) -> Path:
        return self._path

    def save_event(self, event: RatemeterEvent) -> None:
        """Append one peak event row. Flushes immediately — no data lost on crash."""
        self._writer.writerow({
            "timestamp": event.timestamp.isoformat(timespec="milliseconds"),
            "band": event.band_label,
            "amplitude_mv": f"{event.amplitude_mv:.4f}",
            "width_ns": f"{event.width_ns:.2f}" if event.width_ns is not None else "",
            "event_type": event.event_type,
            "transit_time_us": f"{event.transit_time_us:.4f}" if event.transit_time_us is not None else "",
            "velocity_m_s": f"{event.velocity_m_s:.2f}" if event.velocity_m_s is not None else "",
        })
        self._file.flush()

    def close(self) -> None:
        """Flush and close. Safe to call more than once."""
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
