"""
Module: instrument_app.services.pressure_logger
Purpose: File-based pressure data logging with daily rotation, plus the
         read-side support the plot widget needs to backfill history.

How it fits:
- Depends on: pathlib/csv, stdlib only
- Used by:    PressureInterlockPage (log_pressure on each reading, close on
              exit), ui.plots.TimePressureView (read_range for CSV backfill)

Directory layout (mirrors services/daq_logger.py's default_base_dir pattern):
    Recorded Data/Pressures/YYYY/MM/
        YYYY_MM_DD_UHV_Pressure.csv      (columns: timestamp, elapsed_minutes, uhv_torr)
        YYYY_MM_DD_Foreline_Pressure.csv (columns: timestamp, elapsed_minutes, fore_torr)

Files rotate at 3:00 AM daily. UHV is logged on every call; Foreline is
throttled to at most once every 10 seconds (it changes slowly, and this
keeps its file from growing needlessly large).

Public API:
- class PressureLogger(base_dir=None):
    log_pressure(elapsed_minutes, uhv_torr, fore_torr)
    read_range(channel, start_ts, end_ts) -> (times, pressures)
    close()

Changelog:
- 2026-09-23 · 0.1.0 · KC · Replaces services/data_recorder.py (deleted).
  Ported from examples/PressureLogger.py (a different DAQ program's dual
  A/B-channel logger), adapted to this app's single UHV/single Foreline
  gauges. Edited despite CLAUDE.md's "do not touch" note on the pressure
  logging module, with explicit user go-ahead.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

FORELINE_THROTTLE_S = 10.0
ROTATION_HOUR = 3  # rotate log files at 3:00 AM


class PressureLogger:
    """Handles pressure data logging to CSV files with daily rotation at 3 AM."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir) if base_dir is not None else self.default_base_dir()
        self._uhv_file = None
        self._fore_file = None
        self._current_log_date = None
        self._last_foreline_log_time = 0.0

    @staticmethod
    def default_base_dir() -> Path:
        """Standard pressure-log output root, alongside Recorded Data/DAQ etc."""
        return Path(__file__).parent.parent / "Recorded Data" / "Pressures"

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def _get_log_date(self, now: Optional[datetime] = None):
        """Logical date for logging: before 3 AM still counts as the previous day."""
        if now is None:
            now = datetime.now()
        if now.hour < ROTATION_HOUR:
            return (now - timedelta(days=1)).date()
        return now.date()

    def _get_file_paths(self, log_date):
        year_str = str(log_date.year)
        month_str = f"{log_date.month:02d}"
        date_str = f"{log_date.year}_{log_date.month:02d}_{log_date.day:02d}"
        dir_path = self.base_dir / year_str / month_str
        uhv_path = dir_path / f"{date_str}_UHV_Pressure.csv"
        fore_path = dir_path / f"{date_str}_Foreline_Pressure.csv"
        return uhv_path, fore_path

    @staticmethod
    def _ensure_directory(file_path: Path) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _open_file(file_path: Path, file_type: str):
        PressureLogger._ensure_directory(file_path)
        is_new = not file_path.exists()
        f = open(file_path, "a", newline="")
        if is_new:
            if file_type == "uhv":
                f.write("timestamp,elapsed_minutes,uhv_torr\n")
            else:
                f.write("timestamp,elapsed_minutes,fore_torr\n")
            f.flush()
        return f

    def _check_rotation(self, now: Optional[datetime] = None) -> None:
        log_date = self._get_log_date(now)
        if self._current_log_date != log_date:
            self.close()
            uhv_path, fore_path = self._get_file_paths(log_date)
            self._uhv_file = self._open_file(uhv_path, "uhv")
            self._fore_file = self._open_file(fore_path, "fore")
            self._current_log_date = log_date
            self._last_foreline_log_time = 0.0

    def log_pressure(
        self,
        elapsed_minutes: float,
        uhv_torr: Optional[float],
        fore_torr: Optional[float],
    ) -> None:
        """Log one reading. UHV is written every call; Foreline is throttled
        to at most once every FORELINE_THROTTLE_S seconds."""
        self._check_rotation()

        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%S")
        uhv_field = f"{uhv_torr:.6e}" if uhv_torr is not None else ""
        fore_field = f"{fore_torr:.6e}" if fore_torr is not None else ""

        if self._uhv_file:
            self._uhv_file.write(f"{timestamp},{elapsed_minutes:.4f},{uhv_field}\n")
            self._uhv_file.flush()

        current_time = time.time()
        if current_time - self._last_foreline_log_time >= FORELINE_THROTTLE_S:
            if self._fore_file:
                self._fore_file.write(f"{timestamp},{elapsed_minutes:.4f},{fore_field}\n")
                self._fore_file.flush()
            self._last_foreline_log_time = current_time

    # ------------------------------------------------------------------
    # Reading (history backfill for the plot)
    # ------------------------------------------------------------------
    _CHANNEL_SUFFIX = {"uhv": "UHV_Pressure", "foreline": "Foreline_Pressure"}

    def _get_csv_file_paths(self, channel: str, start_date, end_date) -> list[Path]:
        suffix = self._CHANNEL_SUFFIX[channel]
        files = []
        current = start_date
        while current <= end_date:
            year_str = str(current.year)
            month_str = f"{current.month:02d}"
            date_str = f"{current.year}_{current.month:02d}_{current.day:02d}"
            file_path = self.base_dir / year_str / month_str / f"{date_str}_{suffix}.csv"
            if file_path.exists():
                files.append(file_path)
            current += timedelta(days=1)
        return files

    def read_range(
        self, channel: str, start_ts: float, end_ts: float
    ) -> tuple[list[float], list[float]]:
        """Read one channel ('uhv' or 'foreline') from on-disk CSV logs for
        the given Unix-timestamp range. Returns (times, pressures); empty
        lists if nothing is found or a file can't be read."""
        if start_ts > end_ts:
            return [], []

        start_date = datetime.fromtimestamp(start_ts).date()
        end_date = datetime.fromtimestamp(end_ts).date()
        files = self._get_csv_file_paths(channel, start_date, end_date)

        times: list[float] = []
        pressures: list[float] = []
        for file_path in files:
            try:
                with open(file_path, "r") as f:
                    lines = f.readlines()
            except (IOError, OSError):
                continue
            for line in lines[1:]:
                parts = line.strip().split(",")
                if len(parts) < 3 or not parts[2]:
                    continue
                try:
                    unix_time = datetime.fromisoformat(parts[0]).timestamp()
                    pressure = float(parts[2])
                except (ValueError, IndexError):
                    continue
                if start_ts <= unix_time <= end_ts:
                    times.append(unix_time)
                    pressures.append(pressure)
        return times, pressures

    def close(self) -> None:
        if self._uhv_file:
            self._uhv_file.close()
            self._uhv_file = None
        if self._fore_file:
            self._fore_file.close()
            self._fore_file = None

    def __del__(self):
        self.close()
