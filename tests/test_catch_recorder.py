"""
test_catch_recorder.py
-----------------------
Tests for services/catch_recorder.py: raw CSV layout, round trip, sidecar JSON,
index CSV, session summary. No Qt, no hardware.

Run with:
    python -m pytest tests/test_catch_recorder.py
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pytest

from instrument_app.services.catch_classifier import CatchResult
from instrument_app.services.catch_recorder import CatchRecorder, load_pico_csv
from instrument_app.services.daq_models import AcquisitionConfig, DualWaveformRecord

EXAMPLE = Path(__file__).parent / "data" / "catch_example_FIGURE_1.csv.gz"


def _record(n=1000, trace_id=7, pre=0):
    cfg_a = AcquisitionConfig("A", 0.02, "DC", 200, n, pre_trigger_samples=pre)
    cfg_b = AcquisitionConfig("B", 0.05, "DC", 200, n, pre_trigger_samples=pre, probe_factor=10.0)
    rng = np.random.default_rng(0)
    return DualWaveformRecord(
        trace_id=trace_id, timestamp=datetime(2026, 10, 13, 9, 30, 15, 123000),
        voltage_a=rng.standard_normal(n) * 1e-3, voltage_b=rng.standard_normal(n) * 1e-3,
        time_ns=np.arange(n) * 200.0, sample_interval_ns=200,
        config_a=cfg_a, config_b=cfg_b, metadata={"overflow_a": True, "overflow_b": False},
    )


def _result():
    return CatchResult(is_catch=True, reason="ok", period_us=241.8, frequency_hz=4135.6,
                       a_coherence=0.77, b_coherence=0.49)


def _save(rec, record=None, mirror="M1 +350 V, M2 +420 V", kind="catch"):
    return rec.save(record or _record(), _result(), mirror, {"analysis_start_ms": 3.5},
                    {"enabled": True, "threshold_mv": 6.0}, {"triggers": 12}, kind=kind)


def test_csv_layout_matches_picoscope_export(tmp_path):
    path = _save(CatchRecorder(tmp_path, "S1"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "Time,Channel A,Channel B"
    assert lines[1] == "(ms),(mV),(mV)"
    assert lines[2] == ""
    assert len(lines) == 3 + 1000
    assert len(lines[3].split(",")) == 3


def test_round_trip_values_and_time_axis(tmp_path):
    record = _record(n=500)
    path = _save(CatchRecorder(tmp_path, "S1"), record)
    t_ms, a, b = load_pico_csv(path)
    assert t_ms[0] == pytest.approx(0.0) and t_ms[1] == pytest.approx(0.0002)
    np.testing.assert_allclose(a, record.voltage_a * 1000.0, atol=1e-7)
    np.testing.assert_allclose(b, record.voltage_b * 1000.0, atol=1e-7)


def test_pretrigger_samples_shift_time_axis(tmp_path):
    path = _save(CatchRecorder(tmp_path, "S1"), _record(n=100, pre=10))
    t_ms, _, _ = load_pico_csv(path)
    assert t_ms[0] == pytest.approx(-10 * 0.0002)
    assert t_ms[10] == pytest.approx(0.0)


def test_loader_reads_the_real_picoscope_export_with_overrange_markers():
    t_ms, a, b = load_pico_csv(EXAMPLE)
    assert len(t_ms) == 50004
    assert np.isnan(a).sum() == 68 and np.isnan(b).sum() == 12370


def test_sidecar_holds_mirror_voltages_and_settings(tmp_path):
    path = _save(CatchRecorder(tmp_path, "S1"), mirror="front +300 / back +450")
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["mirror_voltages"] == "front +300 / back +450"
    assert meta["result"]["frequency_hz"] == pytest.approx(4135.6)
    assert meta["classifier_config"]["analysis_start_ms"] == 3.5
    assert meta["acquisition"]["channel_b"]["probe_factor"] == 10.0
    assert meta["acquisition"]["sample_interval_ns"] == 200
    assert meta["counters_at_save"]["triggers"] == 12


def test_index_csv_accumulates_rows_with_mirror_text(tmp_path):
    rec = CatchRecorder(tmp_path, "S1")
    _save(rec, mirror="set A")
    _save(rec, mirror="set B")
    rows = list(csv.DictReader(open(tmp_path / "S1" / "index.csv", encoding="utf-8")))
    assert [r["mirror_voltages"] for r in rows] == ["set A", "set B"]
    assert rows[0]["file"].startswith("catch_0001_") and rows[1]["file"].startswith("catch_0002_")


def test_unclassified_traces_are_named_distinctly(tmp_path):
    path = _save(CatchRecorder(tmp_path, "S1"), kind="unclassified")
    assert path.name.startswith("unclassified_0001_")


def test_no_partial_files_left_behind(tmp_path):
    _save(CatchRecorder(tmp_path, "S1"))
    assert not list((tmp_path / "S1").glob("*.tmp"))


def test_session_summary_records_mirror_history(tmp_path):
    rec = CatchRecorder(tmp_path, "S1")
    _save(rec, mirror="set A")
    _save(rec, mirror="set A")
    _save(rec, mirror="set B")
    rec.write_session_summary({"triggers": 50, "catches": 3}, {"x": 1}, {"enabled": True})
    s = json.loads((tmp_path / "S1" / "session.json").read_text(encoding="utf-8"))
    assert s["saved_waveforms"] == 3
    assert [h["mirror_voltages"] for h in s["mirror_voltages_history"]] == ["set A", "set B"]
    assert s["counters"]["triggers"] == 50


def test_idle_session_leaves_no_folder(tmp_path):
    rec = CatchRecorder(tmp_path, "S1")
    rec.write_session_summary({"triggers": 0, "no_trigger_timeouts": 0}, {}, {})
    assert not (tmp_path / "S1").exists()


def test_gzip_csv_reads_identically(tmp_path):
    import gzip
    plain = _save(CatchRecorder(tmp_path, "S1"))
    gz = tmp_path / "copy.csv.gz"
    with open(plain, "rb") as src, gzip.open(gz, "wb") as dst:
        dst.write(src.read())
    for a, b in zip(load_pico_csv(plain), load_pico_csv(gz)):
        np.testing.assert_array_equal(a, b)
