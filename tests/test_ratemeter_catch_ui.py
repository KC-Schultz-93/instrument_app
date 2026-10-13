"""
test_ratemeter_catch_ui.py
---------------------------
Catch-capture mode on the Ratemeter page: controls, config building, settings
persistence, mode switching, and an end-to-end run against a fake scope.
Headless (offscreen Qt), no hardware.

Run with:
    python -m pytest tests/test_ratemeter_catch_ui.py
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
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from instrument_app.pages import ratemeter_page as rp
from instrument_app.services.catch_classifier import CatchClassifierConfig
from instrument_app.services.catch_recorder import CatchRecorder, load_pico_csv
from instrument_app.services.catch_worker import CatchPipeline
from instrument_app.services.daq_channels import DAQChannels
from instrument_app.services.daq_models import DualWaveformRecord

_app = QApplication.instance() or QApplication(sys.argv)

EXAMPLE = Path(__file__).parent / "data" / "catch_example_FIGURE_1.csv.gz"


class FakeScope:
    """Replays queued dual-channel records; remembers which configure path was used."""

    def __init__(self):
        self.is_connected = False
        self.calls = []
        self.records = []

    def connect(self):
        self.is_connected = True

    def disconnect(self):
        self.is_connected = False

    def configure_channel(self, config):
        self.calls.append("single")

    def configure_channels_dual(self, config_a, config_b):
        self.calls.append(("dual", config_a, config_b))

    def set_trigger(self, config, auto_trigger_ms=1000):
        self.calls.append(("trigger", config.channel, auto_trigger_ms))

    def run_block_dual(self, config_a, config_b, abort_check=None):
        if self.records:
            return self.records.pop(0)
        while not (abort_check and abort_check()):
            time.sleep(0.005)
        return None


_state = {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    ini = str(tmp_path / "catch_ui.ini")
    _state["ini"] = ini
    monkeypatch.setattr(rp, "QSettings", lambda *_a: QSettings(ini, QSettings.IniFormat))
    monkeypatch.setattr(rp, "PicoScopeService", FakeScope)
    monkeypatch.setattr(CatchRecorder, "default_base_dir", staticmethod(lambda: tmp_path / "Catch"))
    return tmp_path


def make_page(seed=None):
    s = QSettings(_state["ini"], QSettings.IniFormat)
    s.clear()
    for key, value in (seed or {}).items():
        s.setValue(key, value)
    s.sync()
    return rp.RatemeterPage(DAQChannels())


def _pump(cond, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not cond():
        _app.processEvents()
        time.sleep(0.01)
    _app.processEvents()
    return cond()


def _example_record(config_a, config_b):
    _, a, b = load_pico_csv(EXAMPLE)
    return DualWaveformRecord(
        trace_id=0, timestamp=datetime.now(), voltage_a=a * 1e-3, voltage_b=b * 1e-3,
        time_ns=np.arange(a.size) * 200.0, sample_interval_ns=200,
        config_a=config_a, config_b=config_b, metadata={},
    )


def _noise_record(config_a, config_b, seed):
    rng = np.random.default_rng(seed)
    a = rng.standard_normal(50000) * 2e-4
    a[:10] = 0.007
    return DualWaveformRecord(
        trace_id=0, timestamp=datetime.now(), voltage_a=a, voltage_b=rng.standard_normal(50000) * 2e-5,
        time_ns=np.arange(50000) * 200.0, sample_interval_ns=200,
        config_a=config_a, config_b=config_b, metadata={},
    )


def _arm_for_catch(page, window_ms=10.0, threshold_mv=5.0):
    """Settings matching the first capture: 10 ms window at 200 ns, trigger on A."""
    page.spin_window.setValue(window_ms)
    page.chk_trigger_enable.setChecked(True)
    page.spin_trigger_threshold.setValue(threshold_mv)
    page.chk_catch_mode.setChecked(True)


# --------------------------------------------------------------------------

def test_defaults_match_the_classifier_defaults(env):
    page = make_page()
    assert not page.chk_catch_mode.isChecked()
    cfg = page._build_catch_classifier_config()
    ref = CatchClassifierConfig()
    for field in ("analysis_start_ms", "analysis_end_ms", "min_cycles", "min_period_us",
                  "a_coherence_min", "b_coherence_min", "fold_snr_min", "b_shape_ratio_min"):
        assert getattr(cfg, field) == getattr(ref, field), field
    assert page._tiles["catch"].primary_text() == "Off"
    assert page.edit_mirror_voltages.text() == ""


def test_acq_configs_force_A_and_give_B_its_own_settings(env):
    page = make_page()
    page.combo_channel.setCurrentText("B")                       # would normally select B
    page.combo_catch_b_probe.setCurrentText("x10")
    page.combo_catch_b_range.setCurrentIndex(page.combo_catch_b_range.findData(0.05))
    page.combo_catch_b_coupling.setCurrentText("AC")
    page.chk_catch_b_bw.setChecked(True)
    page.chk_trigger_enable.setChecked(True)
    page.spin_trigger_threshold.setValue(6.0)
    a, b = page._build_catch_acq_configs()
    assert a.channel == "A" and b.channel == "B"
    assert a.trigger_enabled and a.trigger_threshold_v == pytest.approx(0.006)
    assert (b.voltage_range_v, b.probe_factor, b.coupling, b.bandwidth_limit_enabled) == (0.05, 10.0, "AC", True)
    assert (b.sample_interval_ns, b.num_samples) == (a.sample_interval_ns, a.num_samples)


def test_catch_settings_round_trip(env):
    page = make_page()
    page.edit_mirror_voltages.setText("front +300 V / back +450 V")
    page.spin_catch_start_ms.setValue(4.25)
    page.spin_catch_end_ms.setValue(9.0)
    page.spin_catch_min_cycles.setValue(6.5)
    page.spin_catch_a_coherence.setValue(0.55)
    page.spin_catch_b_shape.setValue(0.0)
    page.combo_catch_b_probe.setCurrentText("x0.1")
    page.chk_catch_mode.setChecked(True)
    page._save_settings()

    page2 = rp.RatemeterPage(DAQChannels())
    assert page2.chk_catch_mode.isChecked()
    assert page2.edit_mirror_voltages.text() == "front +300 V / back +450 V"
    cfg = page2._build_catch_classifier_config()
    assert (cfg.analysis_start_ms, cfg.analysis_end_ms, cfg.min_cycles) == (4.25, 9.0, 6.5)
    assert cfg.a_coherence_min == pytest.approx(0.55) and cfg.b_shape_ratio_min == 0.0
    assert page2.combo_catch_b_probe.currentText() == "x0.1"


def test_catch_mode_locks_channel_to_A_and_switches_the_plot_axis(env):
    page = make_page()
    axis = page.plot_widget.getPlotItem().getAxis("right")
    assert not axis.isVisible() and not page._catch_b_curve.isVisible()

    page.combo_channel.setCurrentText("B")
    page.chk_catch_mode.setChecked(True)
    assert page.combo_channel.currentText() == "A" and not page.combo_channel.isEnabled()
    assert axis.isVisible() and page._catch_b_curve.isVisible()
    assert page._tiles["catch"].primary_text().startswith("On")

    # a detection-mode change must not hide channel B's axis while catch mode is on
    page._update_corr_overlay_visibility(False)
    assert axis.isVisible()

    page.chk_catch_mode.setChecked(False)
    assert page.combo_channel.isEnabled()
    assert not axis.isVisible() and not page._catch_b_curve.isVisible()
    assert page._tiles["catch"].primary_text() == "Off"


def test_window_info_warns_when_analysis_window_is_empty_or_short(env):
    page = make_page()
    page.spin_window.setValue(10.0)
    assert "Analysing 3.5" in page.lbl_catch_window_info.text() and "⚠" not in page.lbl_catch_window_info.text()
    page.spin_catch_start_ms.setValue(10.5)
    assert "empty" in page.lbl_catch_window_info.text()
    page.spin_catch_start_ms.setValue(3.5)
    page.spin_window.setValue(3.6)
    assert "⚠" in page.lbl_catch_window_info.text()


def test_catch_mode_refuses_to_start_without_a_trigger(env):
    page = make_page()
    page.chk_trigger_enable.setChecked(False)
    page.chk_catch_mode.setChecked(True)
    page._service.connect()
    page._on_start_clicked()
    assert page._worker is None
    assert "trigger" in page.lbl_status.text().lower() and "Error" in page.lbl_status.text()


def test_ratemeter_mode_is_unchanged_when_catch_is_off(env):
    page = make_page()
    page._service.connect()
    page._on_start_clicked()
    try:
        assert isinstance(page._worker, rp.RatemeterWorker)
        assert "single" in page._service.calls
        assert not any(isinstance(c, tuple) and c[0] == "dual" for c in page._service.calls)
        assert page.btn_record.isEnabled() and page.btn_timed_record.isEnabled()
        assert page.chk_catch_mode.isEnabled() is False        # mode can't change mid-run
    finally:
        page.stop_acquisition()
    assert page.chk_catch_mode.isEnabled()


def test_end_to_end_run_counts_saves_and_labels(env):
    page = make_page()
    _arm_for_catch(page)
    page.edit_mirror_voltages.setText("M1 +350 V, M2 +420 V")
    svc = page._service
    svc.connect()
    cfg_a, cfg_b = page._build_catch_acq_configs()
    svc.records = [
        _example_record(cfg_a, cfg_b),
        _noise_record(cfg_a, cfg_b, 1),
        _noise_record(cfg_a, cfg_b, 2),
        _example_record(cfg_a, cfg_b),
    ]
    page._on_start_clicked()
    try:
        assert isinstance(page._worker, CatchPipeline)
        assert any(isinstance(c, tuple) and c[0] == "dual" for c in svc.calls)
        assert ("trigger", "A", 1000) in svc.calls
        assert not page.btn_record.isEnabled() and not page.btn_timed_record.isEnabled()
        assert "catch capture" in page.lbl_status.text()

        snap = lambda: page._catch_counters.snapshot()
        assert _pump(lambda: snap()["catches"] + snap()["non_catches"] >= 4)
        # change the mirror text while running: later files would get the new text
        page.edit_mirror_voltages.setText("M1 +360 V, M2 +420 V")
        assert page._worker.analysis._mirror_text == "M1 +360 V, M2 +420 V"
    finally:
        page.stop_acquisition()

    c = page._catch_counters.snapshot()
    assert (c["triggers"], c["catches"], c["non_catches"]) == (4, 2, 2)
    stats = {k: v.text() for k, v in page._catch_stat_labels.items()}
    assert (stats["triggers"], stats["catches"], stats["non_catches"]) == ("4", "2", "2")
    assert stats["rate"].startswith("50.00 %")
    assert page.lbl_catch_warn.text() == ""
    assert page.lbl_trace_count.text().strip() == "Triggers:  4"

    sessions = list((env / "Catch").iterdir())
    assert len(sessions) == 1
    files = sorted(sessions[0].glob("catch_*.csv"))
    assert len(files) == 2
    meta = json.loads(files[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["mirror_voltages"] == "M1 +350 V, M2 +420 V"
    assert (sessions[0] / "session.json").exists() and (sessions[0] / "index.csv").exists()
    assert "Stopped" in page.lbl_status.text()


def test_reset_counts_button(env):
    page = make_page()
    page._catch_counters.add(triggers=9, catches=1, non_catches=8)
    page._on_catch_counts(page._catch_counters.snapshot())
    assert page._catch_stat_labels["triggers"].text() == "9"
    page.btn_catch_reset.click()
    assert page._catch_stat_labels["triggers"].text() == "0"
    assert page._catch_stat_labels["rate"].text() == "–"


def test_warnings_show_dropped_timeouts_and_errors(env):
    page = make_page()
    page._on_catch_counts({"triggers": 50, "catches": 1, "non_catches": 40, "dropped": 9,
                           "no_trigger_timeouts": 3, "errors": 2, "elapsed_s": 60.0})
    warn = page.lbl_catch_warn.text()
    assert "9 dropped" in warn and "3 auto-trigger timeouts" in warn and "2 errors" in warn
    page._on_catch_counts({"triggers": 5, "catches": 1, "non_catches": 4, "elapsed_s": 60.0})
    assert page.lbl_catch_warn.text() == ""


def test_bottom_strip_swaps_to_the_catch_dashboard(env):
    page = make_page()
    assert page._bottom_stack.currentIndex() == 0
    page.chk_catch_mode.setChecked(True)
    assert page._bottom_stack.currentIndex() == 1
    page.chk_catch_mode.setChecked(False)
    assert page._bottom_stack.currentIndex() == 0


def test_catch_controls_do_not_make_the_page_taller(env):
    """The page was built to fit short windows (minimum height ~535 px before catch
    mode existed). Catch mode lives in the top bar and bottom strip so it must not
    grow the left rail. Generous bound: font metrics differ between machines."""
    page = make_page()
    assert page.minimumSizeHint().height() < 620


def test_classifier_settings_apply_to_a_running_pipeline(env):
    page = make_page()
    _arm_for_catch(page)
    page._service.connect()
    page._on_start_clicked()
    try:
        page.spin_catch_a_coherence.setValue(0.9)
        assert page._worker.analysis._config.a_coherence_min == pytest.approx(0.9)
    finally:
        page.stop_acquisition()


def test_hardware_config_error_leaves_page_idle(env):
    page = make_page()
    _arm_for_catch(page)
    page._service.connect()

    def boom(*_a, **_k):
        raise RuntimeError("range not supported")
    page._service.configure_channels_dual = boom
    page._on_start_clicked()
    assert page._worker is None
    assert "Hardware config error" in page.lbl_status.text()
    assert not page.sw_run.isChecked()


def test_no_dock_panel_is_wider_than_the_dock(env):
    """The dock sizes itself to its widest page, so one over-wide widget clips the
    right edge of every panel (an unwrapped label once did, at 356 px)."""
    page = make_page()
    widest = max(
        page.dock._stack.widget(i).minimumSizeHint().width()
        for i in range(page.dock._stack.count())
    )
    assert widest <= 300, widest
