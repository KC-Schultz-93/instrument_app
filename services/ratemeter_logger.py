"""
ratemeter_logger.py
-------------------
Append-only CSV logger for ratemeter peak events, plus optional per-segment
echo-validation output (see docs/runt_trigger_plan.md).

One instance per recording session. Created by RatemeterPage when the user
clicks "Record Data"; closed when they stop recording or stop acquisition.

Output directory:
    Recorded Data/Ratemeter/
        <run_id>_events.csv       (always)
        <run_id>_segments.csv     (only when save_raw_traces=True)
        raw/<run_id>/trace_NNNNNN.npz   (only when save_raw_traces=True)

CSV columns, <run_id>_events.csv (one row per detected peak event):
    timestamp, band, amplitude_mv, width_ns, event_type,
    transit_time_us, velocity_m_s

CSV columns, <run_id>_segments.csv (one row per captured segment, independent
of per-peak band membership — a different granularity than the events CSV):
    trace_id, timestamp, echo_validated, echo_reason, echo_lag_us,
    echo_correlation, echo_amp_ratio, partial_batch
"""
from __future__ import annotations

import csv
import dataclasses
import json
from datetime import datetime
from pathlib import Path

import numpy as np

from instrument_app.services.daq_models import RatemeterEvent, WaveformRecord


_CSV_FIELDS = [
    "timestamp",
    "band",
    "amplitude_mv",
    "width_ns",
    "event_type",
    "transit_time_us",
    "velocity_m_s",
]

_SEGMENTS_CSV_FIELDS = [
    "trace_id",
    "timestamp",
    "echo_validated",
    "echo_reason",
    "echo_lag_us",
    "echo_correlation",
    "echo_amp_ratio",
    "partial_batch",
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
    save_raw_traces : bool
        When True, also opens <run_id>_segments.csv and the raw/<run_id>/
        directory for save_segment_result()/save_raw_trace(). When False
        (default), those two methods are no-ops — only the per-peak events
        CSV is written, matching this logger's original behavior.
    """

    def __init__(self, base_dir: Path, run_id: str, save_raw_traces: bool = False) -> None:
        self.run_id = run_id
        self._path = base_dir / f"{run_id}_events.csv"
        base_dir.mkdir(parents=True, exist_ok=True)

        self._file = open(self._path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=_CSV_FIELDS)
        self._writer.writeheader()
        self._file.flush()

        self._segments_file = None
        self._segments_writer = None
        self._raw_dir = None
        if save_raw_traces:
            segments_path = base_dir / f"{run_id}_segments.csv"
            self._segments_file = open(segments_path, "w", newline="", encoding="utf-8")
            self._segments_writer = csv.DictWriter(self._segments_file, fieldnames=_SEGMENTS_CSV_FIELDS)
            self._segments_writer.writeheader()
            self._segments_file.flush()

            self._raw_dir = base_dir / "raw" / run_id
            self._raw_dir.mkdir(parents=True, exist_ok=True)

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

    def save_segment_result(
        self, trace_id: int, timestamp: datetime, result, partial_batch: bool = False,
    ) -> None:
        """
        Append one per-segment echo-validation row. No-op unless this logger
        was constructed with save_raw_traces=True.

        Parameters
        ----------
        result : EchoValidationResult
        partial_batch : bool
            From the segment's WaveformRecord.metadata["partial_batch"] — True
            when this segment came from a rapid-block batch that stopped
            early on the ready-timeout fallback (see PicoScopeService.run_rapid_block).
        """
        if self._segments_writer is None:
            return
        self._segments_writer.writerow({
            "trace_id": trace_id,
            "timestamp": timestamp.isoformat(timespec="milliseconds"),
            "echo_validated": result.accepted,
            "echo_reason": result.reason,
            "echo_lag_us": f"{result.echo_lag_us:.4f}" if result.echo_lag_us is not None else "",
            "echo_correlation": f"{result.echo_correlation:.4f}" if result.echo_correlation is not None else "",
            "echo_amp_ratio": f"{result.amplitude_ratio:.4f}" if result.amplitude_ratio is not None else "",
            "partial_batch": bool(partial_batch),
        })
        self._segments_file.flush()

    def save_raw_trace(self, record: WaveformRecord) -> None:
        """
        Save one validated segment's waveform as a compressed .npz file,
        following DAQLogger.save_raw()'s layout (voltage, time_ns, a JSON
        "meta" blob merging record.metadata). No-op unless this logger was
        constructed with save_raw_traces=True.
        """
        if self._raw_dir is None:
            return
        filename = self._raw_dir / f"trace_{record.trace_id:06d}.npz"

        meta_dict = {
            "trace_id": record.trace_id,
            "run_id": record.run_id,
            "timestamp": record.timestamp.isoformat(),
            "sample_interval_ns": record.sample_interval_ns,
            "config": dataclasses.asdict(record.config) if record.config is not None else None,
            **record.metadata,
        }
        meta_arr = np.array([json.dumps(meta_dict)], dtype=object)

        np.savez_compressed(
            filename,
            voltage=record.voltage,
            time_ns=record.time_ns,
            meta=meta_arr,
        )

    def close(self) -> None:
        """Flush and close. Safe to call more than once."""
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
        if self._segments_file:
            self._segments_file.flush()
            self._segments_file.close()
            self._segments_file = None
