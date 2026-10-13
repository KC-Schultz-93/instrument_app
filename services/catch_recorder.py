"""
catch_recorder.py
------------------
Saves kept (caught-particle) waveforms from the Ratemeter's catch-capture mode.

One folder per capture session:

    Recorded Data/Catch/<session_id>/
        catch_0001_<timestamp>.csv    raw A+B waveform, PicoScope-export layout
        catch_0001_<timestamp>.json   mirror voltages, measured features, settings
        index.csv                     one row per saved waveform (easy to sort/filter)
        session.json                  counts and settings, written when the run stops

The waveform CSV mirrors the PicoScope 7 export (name row, unit row, blank row,
then Time(ms), Channel A(mV), Channel B(mV)) so existing scripts and
PicoScope-trained habits keep working. Channel B is the scope input in true
(probe-scaled) mV; converting it to charge is left to downstream analysis.

Files are written to a temporary name and renamed, so a crash never leaves a
half-written waveform. No Qt, no hardware.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from instrument_app.services.catch_classifier import CatchResult
from instrument_app.services.daq_models import DualWaveformRecord

_LOG_FIELDS = [
    "file", "timestamp", "mirror_voltages", "kind", "frequency_hz", "period_us",
    "a_coherence", "b_coherence", "trace_id", "triggers_so_far",
]


def load_pico_csv(path):
    """Read a PicoScope-style CSV (as exported by PicoScope 7 or by this recorder);
    a path ending in .gz is read compressed.

    Returns (time_ms, channel_a, channel_b) as float arrays. Overrange markers
    ("∞" / "-∞") become NaN. Needs only numpy."""
    times: List[float] = []
    a_vals: List[float] = []
    b_vals: List[float] = []

    def _f(text: str) -> float:
        try:
            return float(text)
        except ValueError:
            return float("nan")

    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            try:
                t = float(row[0])
            except ValueError:
                continue                    # name / unit / blank header rows
            times.append(t)
            a_vals.append(_f(row[1]))
            b_vals.append(_f(row[2]))
    return np.array(times), np.array(a_vals), np.array(b_vals)


class CatchRecorder:
    """Writes caught waveforms for one capture session."""

    def __init__(self, base_dir: Path, session_id: Optional[str] = None) -> None:
        self._base_dir = Path(base_dir)
        self.session_id = session_id or self.make_session_id()
        self._dir = self._base_dir / self.session_id
        self._started = datetime.now()
        self._saved = 0
        self._mirror_history: List[Dict[str, str]] = []

    # ------------------------------------------------------------------

    @staticmethod
    def make_session_id() -> str:
        return datetime.now().strftime("%Y_%m_%d_%H%M%S")

    @staticmethod
    def default_base_dir() -> Path:
        return Path(__file__).parent.parent / "Recorded Data" / "Catch"

    @property
    def session_dir(self) -> Path:
        return self._dir

    @property
    def saved_count(self) -> int:
        return self._saved

    # ------------------------------------------------------------------

    def save(
        self,
        record: DualWaveformRecord,
        result: CatchResult,
        mirror_voltages: str,
        classifier_config: dict,
        trigger: dict,
        counters: dict,
        kind: str = "catch",
    ) -> Path:
        """Save one waveform. `kind` is "catch", or "unclassified" when the
        classifier itself failed (kept rather than lost). Returns the CSV path."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._saved += 1
        stamp = record.timestamp.strftime("%Y_%m_%d_%H%M%S_%f")[:-3]
        stem = f"{kind}_{self._saved:04d}_{stamp}"
        csv_path = self._dir / f"{stem}.csv"

        self._write_waveform_csv(csv_path, record)

        meta = {
            "file": csv_path.name,
            "kind": kind,
            "timestamp": record.timestamp.isoformat(timespec="milliseconds"),
            "trace_id": record.trace_id,
            "mirror_voltages": mirror_voltages,
            "result": result.to_dict(),
            "classifier_config": classifier_config,
            "trigger": trigger,
            "acquisition": {
                "sample_interval_ns": record.sample_interval_ns,
                "num_samples": int(record.voltage_a.size),
                "pre_trigger_samples": record.config_a.pre_trigger_samples,
                "channel_a": _channel_meta(record.config_a),
                "channel_b": _channel_meta(record.config_b),
                "overflow_a": record.metadata.get("overflow_a"),
                "overflow_b": record.metadata.get("overflow_b"),
            },
            "counters_at_save": counters,
            "units": {"time": "ms", "channel_a": "mV", "channel_b": "mV (true, probe-scaled)"},
        }
        self._atomic_write_text(csv_path.with_suffix(".json"), json.dumps(meta, indent=2))

        self._append_log_row({
            "file": csv_path.name,
            "timestamp": meta["timestamp"],
            "mirror_voltages": mirror_voltages,
            "kind": kind,
            "frequency_hz": _fmt(result.frequency_hz),
            "period_us": _fmt(result.period_us),
            "a_coherence": _fmt(result.a_coherence),
            "b_coherence": _fmt(result.b_coherence),
            "trace_id": record.trace_id,
            "triggers_so_far": counters.get("triggers"),
        })
        if not self._mirror_history or self._mirror_history[-1]["mirror_voltages"] != mirror_voltages:
            self._mirror_history.append(
                {"from_file": csv_path.name, "mirror_voltages": mirror_voltages}
            )
        return csv_path

    def write_session_summary(self, counters: dict, classifier_config: dict, trigger: dict) -> None:
        """Write session.json. Skipped when nothing was triggered, so idle runs
        leave no empty folders behind."""
        if not counters.get("triggers") and not counters.get("no_trigger_timeouts"):
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "session_id": self.session_id,
            "started": self._started.isoformat(timespec="seconds"),
            "ended": datetime.now().isoformat(timespec="seconds"),
            "saved_waveforms": self._saved,
            "counters": counters,
            "classifier_config": classifier_config,
            "trigger": trigger,
            "mirror_voltages_history": self._mirror_history,
        }
        self._atomic_write_text(self._dir / "session.json", json.dumps(summary, indent=2))

    # ------------------------------------------------------------------

    @staticmethod
    def _write_waveform_csv(path: Path, record: DualWaveformRecord) -> None:
        n = record.voltage_a.size
        dt_ms = record.sample_interval_ns / 1e6
        t_ms = (np.arange(n) - record.config_a.pre_trigger_samples) * dt_ms
        data = np.column_stack([t_ms, record.voltage_a * 1000.0, record.voltage_b * 1000.0])
        tmp = path.with_name(path.name + ".tmp")
        np.savetxt(
            tmp, data, fmt="%.8f", delimiter=",", comments="",
            header="Time,Channel A,Channel B\n(ms),(mV),(mV)\n",
        )
        os.replace(tmp, path)

    def _append_log_row(self, row: dict) -> None:
        path = self._dir / "index.csv"
        write_header = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def _channel_meta(cfg) -> dict:
    return {
        "channel": cfg.channel,
        "range_v_native": cfg.voltage_range_v,
        "probe_factor": cfg.probe_factor,
        "coupling": cfg.coupling,
        "bandwidth_limit": cfg.bandwidth_limit_enabled,
    }


def _fmt(value) -> str:
    return "" if value is None else f"{value:.4f}"
