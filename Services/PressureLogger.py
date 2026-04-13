"""
PressureLogger - File-based pressure data logging with daily rotation.

Logs UHV and Foreline pressure readings to CSV files organized by date.
Files rotate daily at 3:00 AM.

Directory structure:
    Recorded Data/Recorded Pressures/YYYY/MM/
        YYYY_MM_DD_UHV_Pressure.csv
        YYYY_MM_DD_Foreline_Pressure.csv
"""

import os
import time
from datetime import datetime, time as dt_time
from pathlib import Path


class PressureLogger:
    """Handles pressure data logging to CSV files with daily rotation at 3 AM."""

    ROTATION_HOUR = 3  # Rotate files at 3:00 AM

    def __init__(self, base_dir=None):
        """
        Initialize the pressure logger.

        Args:
            base_dir: Base directory for log files. Defaults to
                      'Recorded Data/Recorded Pressures' in the script's directory.
        """
        if base_dir is None:
            # Default to script directory / Recorded Data / Recorded Pressures
            script_dir = Path(__file__).parent.parent
            base_dir = script_dir / "Recorded Data" / "Recorded Pressures"

        self.base_dir = Path(base_dir)
        self._uhv_file = None
        self._fore_file = None
        self._current_log_date = None
        self._last_foreline_log_time = 0  # Track last foreline write time for throttling

    def _get_log_date(self, now=None):
        """
        Get the logical date for logging purposes.

        Before 3 AM, we're still on the previous day's log.
        At/after 3 AM, we're on the current day's log.
        """
        if now is None:
            now = datetime.now()

        if now.hour < self.ROTATION_HOUR:
            # Before 3 AM - use previous day
            from datetime import timedelta
            return (now - timedelta(days=1)).date()
        else:
            return now.date()

    def _get_file_paths(self, log_date):
        """Get the file paths for UHV and Foreline logs for a given date."""
        year_str = str(log_date.year)
        month_str = f"{log_date.month:02d}"
        date_str = f"{log_date.year}_{log_date.month:02d}_{log_date.day:02d}"

        dir_path = self.base_dir / year_str / month_str

        uhv_path = dir_path / f"{date_str}_UHV_Pressure.csv"
        fore_path = dir_path / f"{date_str}_Foreline_Pressure.csv"

        return uhv_path, fore_path

    def _ensure_directory(self, file_path):
        """Create directory for file if it doesn't exist."""
        file_path.parent.mkdir(parents=True, exist_ok=True)

    def _open_file(self, file_path):
        """Open a log file, creating with header if new."""
        self._ensure_directory(file_path)

        is_new = not file_path.exists()
        f = open(file_path, 'a', newline='')

        if is_new:
            f.write("timestamp,elapsed_minutes,pressure_torr\n")
            f.flush()

        return f

    def _check_rotation(self):
        """Check if we need to rotate to new log files."""
        log_date = self._get_log_date()

        if self._current_log_date != log_date:
            # Close existing files
            self.close()

            # Open new files
            uhv_path, fore_path = self._get_file_paths(log_date)
            self._uhv_file = self._open_file(uhv_path)
            self._fore_file = self._open_file(fore_path)
            self._current_log_date = log_date
            self._last_foreline_log_time = 0  # Reset throttle on rotation

    def log_pressure(self, elapsed_minutes, uhv_torr, fore_torr):
        """
        Log pressure readings to files.

        Args:
            elapsed_minutes: Elapsed time in minutes (from Arduino timestamp)
            uhv_torr: UHV pressure in Torr
            fore_torr: Foreline pressure in Torr
        """
        self._check_rotation()

        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%S")

        # Write UHV (always)
        if self._uhv_file:
            self._uhv_file.write(f"{timestamp},{elapsed_minutes:.4f},{uhv_torr:.6e}\n")
            self._uhv_file.flush()

        # Write Foreline (throttled to once every 10 seconds)
        current_time = time.time()
        if current_time - self._last_foreline_log_time >= 10.0:
            if self._fore_file:
                self._fore_file.write(f"{timestamp},{elapsed_minutes:.4f},{fore_torr:.6e}\n")
                self._fore_file.flush()
            self._last_foreline_log_time = current_time

    def close(self):
        """Close all open log files."""
        if self._uhv_file:
            self._uhv_file.close()
            self._uhv_file = None

        if self._fore_file:
            self._fore_file.close()
            self._fore_file = None

    def __del__(self):
        """Ensure files are closed on deletion."""
        self.close()
