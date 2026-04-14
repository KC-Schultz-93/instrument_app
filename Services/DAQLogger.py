"""
DAQLogger.py
------------
Manages all file output for one DAQ acquisition run.

Directory layout created at run start:
    Recorded Data/DAQ/
        raw/<run_id>/
            trace_000001.npz
            trace_000002.npz
            ...
        reduced/
            <run_id>_events.csv
        metadata/
            <run_id>.json
        logs/
            <run_id>.log

One DAQLogger instance per run. Create it when the run starts, call close()
when the run ends (or on window close). The worker thread calls save_raw()
and save_event_summary(); the main thread calls write_metadata() and close()
only after the worker has stopped.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import logging
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Optional

from Services.DAQModels import EventSummary, WaveformRecord


# CSV column order for the reduced events file
_CSV_FIELDS = [
    "trace_id",
    "run_id",
    "timestamp",
    "accepted",
    "classification",
    "baseline_mean_v",
    "baseline_rms_v",
    "signal_max_v",
    "signal_min_v",
    "num_peaks",
    "mean_peak_height_v",
    "mean_peak_spacing_ns",
    "notes",
]


class DAQLogger:
    """
    File I/O manager for one DAQ run.

    Parameters
    ----------
    base_dir : Path
        Root DAQ output directory, e.g. ``repo_root / "Recorded Data" / "DAQ"``.
    run_id : str
        Unique run identifier string (use make_run_id() to generate one).
    """

    def __init__(self, base_dir: Path, run_id: str) -> None:
        self.run_id = run_id

        self._raw_dir: Path = base_dir / "raw" / run_id
        self._reduced_path: Path = base_dir / "reduced" / f"{run_id}_events.csv"
        self._meta_path: Path = base_dir / "metadata" / f"{run_id}.json"
        self._log_path: Path = base_dir / "logs" / f"{run_id}.log"

        self._csv_file = None
        self._csv_writer: Optional[csv.DictWriter] = None

        self._create_directories()
        self._logger = self._init_logger()
        self._init_csv()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @staticmethod
    def make_run_id() -> str:
        """Generate a run ID string from the current datetime: YYYY_MM_DD_HHmmss."""
        return datetime.now().strftime("%Y_%m_%d_%H%M%S")

    @staticmethod
    def default_base_dir() -> Path:
        """Return the standard DAQ output root relative to the repo root."""
        return Path(__file__).parent.parent / "Recorded Data" / "DAQ"

    def save_raw(self, record: WaveformRecord) -> None:
        """
        Save one WaveformRecord as a compressed .npz file.

        Saved arrays:
            voltage   — float64, volts
            time_ns   — float64, nanoseconds
            meta      — 1-element object array containing a JSON string with
                        trace_id, run_id, timestamp, sample_interval_ns,
                        and the full acquisition config dict.
        """
        filename = self._raw_dir / f"trace_{record.trace_id:06d}.npz"

        meta_dict = {
            "trace_id": record.trace_id,
            "run_id": record.run_id,
            "timestamp": record.timestamp.isoformat(),
            "sample_interval_ns": record.sample_interval_ns,
            "config": dataclasses.asdict(record.config),
            **record.metadata,
        }
        meta_arr = np.array([json.dumps(meta_dict)], dtype=object)

        np.savez_compressed(
            filename,
            voltage=record.voltage,
            time_ns=record.time_ns,
            meta=meta_arr,
        )

    def save_event_summary(self, summary: EventSummary) -> None:
        """Append one row to the reduced events CSV."""
        if self._csv_writer is None:
            return
        row = {
            "trace_id": summary.trace_id,
            "run_id": summary.run_id,
            "timestamp": summary.timestamp.isoformat(),
            "accepted": summary.accepted,
            "classification": summary.classification,
            "baseline_mean_v": summary.baseline_mean_v,
            "baseline_rms_v": summary.baseline_rms_v,
            "signal_max_v": summary.signal_max_v,
            "signal_min_v": summary.signal_min_v,
            "num_peaks": summary.num_peaks,
            "mean_peak_height_v": summary.mean_peak_height_v,
            "mean_peak_spacing_ns": summary.mean_peak_spacing_ns,
            "notes": summary.notes,
        }
        self._csv_writer.writerow(row)
        self._csv_file.flush()

    def write_metadata(self, meta: dict) -> None:
        """Write (or overwrite) the run metadata JSON file."""
        with open(self._meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)

    def log(self, msg: str, level: str = "info") -> None:
        """Write a message to the run .log file."""
        getattr(self._logger, level.lower(), self._logger.info)(msg)

    def close(self) -> None:
        """Flush and close file handles. Call after the worker thread has stopped."""
        if self._csv_file is not None:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

        # Remove the file handler to avoid duplicate log entries if re-used
        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_directories(self) -> None:
        for d in [
            self._raw_dir,
            self._reduced_path.parent,
            self._meta_path.parent,
            self._log_path.parent,
        ]:
            d.mkdir(parents=True, exist_ok=True)

    def _init_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"DAQ.{self.run_id}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # don't bleed into root logger

        handler = logging.FileHandler(self._log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
        )
        logger.addHandler(handler)
        return logger

    def _init_csv(self) -> None:
        """Open the reduced events CSV and write the header row."""
        self._csv_file = open(
            self._reduced_path, "w", newline="", encoding="utf-8"
        )
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=_CSV_FIELDS)
        self._csv_writer.writeheader()
        self._csv_file.flush()
