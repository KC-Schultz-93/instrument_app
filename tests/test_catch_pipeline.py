"""
test_catch_pipeline.py
-----------------------
Threaded catch pipeline against a fake scope (no hardware): counts, saved
files, queue-full handling, prompt stop, and failure paths. Headless Qt.

Run with:
    python -m pytest tests/test_catch_pipeline.py
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pytest
from PyQt5.QtWidgets import QApplication

from instrument_app.services import catch_worker as cw
from instrument_app.services.catch_classifier import CatchClassifierConfig
from instrument_app.services.catch_recorder import CatchRecorder, load_pico_csv
from instrument_app.services.daq_models import AcquisitionConfig, DualWaveformRecord

# Must be a QApplication (not QCoreApplication): other test modules in the same
# pytest process create widgets and reuse whichever instance exists first.
_app = QApplication.instance() or QApplication(sys.argv)

EXAMPLE = Path(__file__).parent / "data" / "catch_example_FIGURE_1.csv.gz"
N = 50004
CFG_A = AcquisitionConfig("A", 0.02, "DC", 200, N, True, 0.005, "RISING")
CFG_B = AcquisitionConfig("B", 0.05, "DC", 200, N)
TRIGGER = cw.TriggerInfo(enabled=True, threshold_v=0.005, direction="RISING", auto_timeout_ms=0)


def _rec(a_v, b_v):
    return DualWaveformRecord(
        trace_id=0, timestamp=datetime.now(), voltage_a=a_v, voltage_b=b_v,
        time_ns=np.arange(a_v.size) * 200.0, sample_interval_ns=200,
        config_a=CFG_A, config_b=CFG_B, metadata={"overflow_a": True, "overflow_b": True},
    )


@pytest.fixture(scope="module")
def real():
    _, a, b = load_pico_csv(EXAMPLE)
    return a * 1e-3, b * 1e-3        # mV -> V (B unit is arbitrary here)


def _noise_record(seed=0):
    rng = np.random.default_rng(seed)
    a = rng.standard_normal(N) * 2e-4
    a[:10] = 0.006                   # real trigger at the start
    return _rec(a, rng.standard_normal(N) * 2e-5)


def _timeout_record():
    return _rec(np.zeros(N), np.zeros(N))       # A never reached the trigger level


class FakeService:
    """Replays a list of records, then waits for a trigger that never comes."""

    def __init__(self, records, delay_s=0.0):
        self._records = list(records)
        self._delay = delay_s

    def run_block_dual(self, cfg_a, cfg_b, abort_check=None):
        if self._records:
            if self._delay:
                time.sleep(self._delay)
            return self._records.pop(0)
        while not (abort_check and abort_check()):
            time.sleep(0.005)
        return None


def _run(tmp_path, service, wait_for, mirror="M1 +350 V", timeout=30.0, **kw):
    recorder = CatchRecorder(tmp_path, "S1")
    pipe = cw.CatchPipeline(
        service, CFG_A, CFG_B, TRIGGER, CatchClassifierConfig(), recorder,
        mirror_voltages=mirror, **kw,
    )
    seen = {"traces": [], "errors": [], "saved": []}
    pipe.trace_ready.connect(lambda r, res: seen["traces"].append(res))
    pipe.error_occurred.connect(seen["errors"].append)
    pipe.catch_saved.connect(seen["saved"].append)
    pipe.start()
    deadline = time.time() + timeout
    while time.time() < deadline and not wait_for(pipe.counters.snapshot()):
        _app.processEvents()
        time.sleep(0.01)
    t0 = time.time()
    pipe.request_stop()
    finished = pipe.wait(10000)
    stop_s = time.time() - t0
    for _ in range(20):
        _app.processEvents()
    return pipe, seen, finished, stop_s


def test_counts_and_saved_files(tmp_path, real):
    a, b = real
    records = [_rec(a, b), _noise_record(1), _noise_record(2), _timeout_record(), _rec(a, b)]
    pipe, seen, finished, _ = _run(
        tmp_path, FakeService(records),
        wait_for=lambda c: c["catches"] + c["non_catches"] >= 4,
    )
    assert finished
    c = pipe.counters.snapshot()
    assert (c["triggers"], c["catches"], c["non_catches"]) == (4, 2, 2)
    assert c["no_trigger_timeouts"] == 1 and c["dropped"] == 0 and c["errors"] == 0

    files = sorted((tmp_path / "S1").glob("catch_*.csv"))
    assert len(files) == 2 and len(seen["saved"]) == 2
    t_ms, a_mv, _ = load_pico_csv(files[0])
    assert len(t_ms) == N
    meta = json.loads(files[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["mirror_voltages"] == "M1 +350 V"
    assert meta["result"]["is_catch"] is True
    assert 4000 < meta["result"]["frequency_hz"] < 4300

    summary = json.loads((tmp_path / "S1" / "session.json").read_text(encoding="utf-8"))
    assert summary["counters"]["catches"] == 2
    assert len(seen["traces"]) == 4        # the timeout trace is not analysed


def test_only_catches_hit_the_disk(tmp_path):
    pipe, _, finished, _ = _run(
        tmp_path, FakeService([_noise_record(i) for i in range(6)]),
        wait_for=lambda c: c["non_catches"] >= 6,
    )
    assert finished
    assert pipe.counters.snapshot()["catches"] == 0
    assert not list((tmp_path / "S1").glob("catch_*.csv")) if (tmp_path / "S1").exists() else True


def test_stop_is_prompt_while_waiting_for_a_trigger(tmp_path):
    pipe, _, finished, stop_s = _run(
        tmp_path, FakeService([]), wait_for=lambda c: False, timeout=0.3
    )
    assert finished and stop_s < 3.0


def test_queue_full_drops_are_counted_and_acquisition_never_blocks(tmp_path, monkeypatch):
    def slow_classify(*_a, **_k):
        time.sleep(0.25)
        return cw.CatchResult(is_catch=False, reason="slow")
    monkeypatch.setattr(cw, "classify_catch", slow_classify)

    records = [_noise_record(i) for i in range(25)]
    pipe, _, finished, _ = _run(
        tmp_path, FakeService(records),
        wait_for=lambda c: c["triggers"] >= 25 and c["non_catches"] + c["dropped"] >= 25,
        timeout=60.0,
    )
    assert finished
    c = pipe.counters.snapshot()
    assert c["triggers"] == 25
    assert c["dropped"] > 0
    assert c["non_catches"] + c["dropped"] == 25      # every trigger is accounted for


def test_classifier_crash_keeps_the_trace(tmp_path, monkeypatch, real):
    def boom(*_a, **_k):
        raise RuntimeError("bug")
    monkeypatch.setattr(cw, "classify_catch", boom)
    a, b = real
    pipe, seen, finished, _ = _run(
        tmp_path, FakeService([_rec(a, b)]), wait_for=lambda c: c["errors"] >= 1
    )
    assert finished
    c = pipe.counters.snapshot()
    assert c["errors"] == 1 and c["catches"] == 0 and c["non_catches"] == 0
    assert len(list((tmp_path / "S1").glob("unclassified_*.csv"))) == 1
    assert any("trace kept" in e for e in seen["errors"])


def test_save_failure_is_counted_and_run_continues(tmp_path, monkeypatch, real):
    a, b = real
    monkeypatch.setattr(
        CatchRecorder, "save", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full"))
    )
    pipe, seen, finished, _ = _run(
        tmp_path, FakeService([_rec(a, b), _noise_record(3)]),
        wait_for=lambda c: c["catches"] + c["non_catches"] >= 2,
    )
    assert finished
    c = pipe.counters.snapshot()
    assert c["errors"] == 1 and c["catches"] == 1 and c["non_catches"] == 1
    assert any("disk full" in e for e in seen["errors"])


def test_mirror_voltage_text_can_change_between_saves(tmp_path, real):
    a, b = real
    recorder = CatchRecorder(tmp_path, "S1")
    worker = cw.CatchAnalysisWorker(
        None, cw.CatchCounters(), recorder, CatchClassifierConfig(), TRIGGER
    )
    worker.set_mirror_voltages("first setting")
    worker._handle(_rec(a, b))
    worker.set_mirror_voltages("second setting")
    worker._handle(_rec(a, b))
    metas = [json.loads(p.read_text(encoding="utf-8"))
             for p in sorted((tmp_path / "S1").glob("catch_*.json"))]
    assert [m["mirror_voltages"] for m in metas] == ["first setting", "second setting"]


def test_classifier_settings_can_change_while_running(tmp_path, real):
    a, b = real
    worker = cw.CatchAnalysisWorker(
        None, cw.CatchCounters(), CatchRecorder(tmp_path, "S1"), CatchClassifierConfig(), TRIGGER
    )
    worker._handle(_rec(a, b))
    assert worker._counters.snapshot()["catches"] == 1
    worker.set_classifier_config(CatchClassifierConfig(a_coherence_min=0.99))
    worker._handle(_rec(a, b))
    assert worker._counters.snapshot()["non_catches"] == 1


def test_counters_reset():
    c = cw.CatchCounters()
    c.add(triggers=5, catches=2)
    c.reset()
    assert c.snapshot()["triggers"] == 0 and c.snapshot()["catches"] == 0
