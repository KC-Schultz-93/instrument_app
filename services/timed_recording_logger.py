"""
timed_recording_logger.py
--------------------------
Append-only CSV logger for timed-recording sessions on the Ratemeter page.

Unlike RatemeterLogger/DAQLogger (one fresh file per run, opened once with a
header), these two CSVs persist across many recording sessions:

    Recorded Data/Ratemeter/
        recordings.csv   -- one row per completed timed recording
        peaks.csv         -- one row per raw detected peak in that recording

Each write happens exactly once, after the caller has accepted the
post-recording metadata dialog; nothing is written on cancel. The caller is
responsible for buffering PeakRecords in memory during the recording window.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import List

from instrument_app.services.daq_models import PeakRecord, TimedRecordingSummary


_RECORDINGS_CSV_FIELDS = [
    "recording_id", "timestamp", "description", "vpp", "frequency_hz",
    "duration_s", "total_peak_count", "channel", "voltage_range_v",
    "coupling", "sample_interval_ns", "window_duration_ms",
    "trigger_enabled", "trigger_threshold_mv",
]

_PEAKS_CSV_FIELDS = ["recording_id", "peak_amplitude_mv", "peak_time_ns"]


class TimedRecordingLogger:
    """Writes completed timed-recording sessions to two persistent CSVs."""

    def __init__(self, base_dir: Path) -> None:
        base_dir.mkdir(parents=True, exist_ok=True)
        self._recordings_path = base_dir / "recordings.csv"
        self._peaks_path = base_dir / "peaks.csv"

    @staticmethod
    def make_run_id() -> str:
        """Generate a recording ID from the current datetime: YYYY_MM_DD_HHmmss."""
        return datetime.now().strftime("%Y_%m_%d_%H%M%S")

    @staticmethod
    def default_base_dir() -> Path:
        return Path(__file__).parent.parent / "Recorded Data" / "Ratemeter"

    def save_recording(self, summary: TimedRecordingSummary) -> None:
        self._append_row(self._recordings_path, _RECORDINGS_CSV_FIELDS, {
            "recording_id": summary.recording_id,
            "timestamp": summary.timestamp.isoformat(timespec="milliseconds"),
            "description": summary.description,
            "vpp": f"{summary.vpp:.4f}",
            "frequency_hz": f"{summary.frequency_hz:.4f}",
            "duration_s": f"{summary.duration_s:.2f}",
            "total_peak_count": summary.total_peak_count,
            "channel": summary.channel,
            "voltage_range_v": summary.voltage_range_v,
            "coupling": summary.coupling,
            "sample_interval_ns": summary.sample_interval_ns,
            "window_duration_ms": summary.window_duration_ms,
            "trigger_enabled": summary.trigger_enabled,
            "trigger_threshold_mv": summary.trigger_threshold_mv,
        })

    def save_peaks(self, recording_id: str, peaks: List[PeakRecord]) -> None:
        if not peaks:
            return
        write_header = not self._peaks_path.exists()
        with open(self._peaks_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_PEAKS_CSV_FIELDS)
            if write_header:
                writer.writeheader()
            for peak in peaks:
                writer.writerow({
                    "recording_id": recording_id,
                    "peak_amplitude_mv": f"{peak.amplitude_v * 1000.0:.4f}",
                    "peak_time_ns": f"{peak.time_ns:.2f}",
                })

    def _append_row(self, path: Path, fields: List[str], row: dict) -> None:
        write_header = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
