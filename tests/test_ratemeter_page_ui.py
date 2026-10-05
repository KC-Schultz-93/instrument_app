"""
test_ratemeter_page_ui.py
--------------------------
Characterization tests for RatemeterPage. They pin down config building and
QSettings persistence so the UI refactor (tiles + docked panels) cannot change
acquisition behaviour. Headless (offscreen Qt), no hardware: PicoScopeService
is replaced by a fake.

Run with:
    python -m pytest tests/test_ratemeter_page_ui.py
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from instrument_app.pages import ratemeter_page as rp
from instrument_app.services.daq_channels import DAQChannels

_app = QApplication.instance() or QApplication(sys.argv)

# Keys written by RatemeterPage._save_settings(), excluding the legacy
# section-expanded keys that the refactor stops using.
EXPECTED_KEYS = {
    "ratemeter/probe", "ratemeter/voltage_range_v", "ratemeter/sample_interval_ns",
    "ratemeter/window_duration_ms", "ratemeter/coupling", "ratemeter/trigger_enabled",
    "ratemeter/trigger_threshold_mv", "ratemeter/trigger_direction",
    "ratemeter/trigger_auto_ms", "ratemeter/rate_averaging_s", "ratemeter/trend_window_s",
    "ratemeter/width_rel_height_idx", "ratemeter/bandwidth_limit_enabled",
    "ratemeter/captures_per_batch", "ratemeter/detection_mode", "ratemeter/mf_pulse_us",
    "ratemeter/mf_pulse_min_us", "ratemeter/mf_pulse_max_us", "ratemeter/mf_scale_factors",
    "ratemeter/mf_template_path", "ratemeter/mf_use_empirical", "ratemeter/mf_polarity",
    "ratemeter/mf_corr_threshold", "ratemeter/mf_min_spacing_us", "ratemeter/bands",
    "ratemeter/timed_recording_duration_s",
}


class FakeService:
    """Stands in for PicoScopeService; never touches the driver."""

    def __init__(self):
        self.is_connected = False

    def connect(self):
        self.is_connected = True

    def disconnect(self):
        self.is_connected = False

    def configure_channel(self, config):
        pass

    def set_trigger(self, config, auto_trigger_ms=1000):
        pass


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    """Isolated ini-backed QSettings so the real app settings are never touched."""
    ini = str(tmp_path / "ratemeter_test.ini")
    monkeypatch.setattr(rp, "QSettings", lambda *_a: QSettings(ini, QSettings.IniFormat))
    monkeypatch.setattr(rp, "PicoScopeService", FakeService)
    _state["ini"] = ini


_state = {}


def make_page(seed=None):
    s = QSettings(_state["ini"], QSettings.IniFormat)
    s.clear()
    for key, value in (seed or {}).items():
        s.setValue(key, value)
    s.sync()
    return rp.RatemeterPage(DAQChannels())


BANDS = json.dumps([
    {"label": "Band 1", "low_mv": 1.0, "high_mv": 3.0, "color": "#4fc3f7", "transit_min_width_ns": 800.0},
    {"label": "Band 2", "low_mv": 3.0, "high_mv": 6.0, "color": "#ff8a65", "transit_min_width_ns": None},
    {"label": "Band 3", "low_mv": 6.0, "high_mv": 12.0, "color": "#81c784", "transit_min_width_ns": 400.0},
])

CASES = [
    # probe, native range, interval ns, trigger on, direction, MF on
    ("x1", 0.02, 200, False, "Rising", False),
    ("x10", 0.05, 10, True, "Falling", True),
    ("x0.1", 0.1, 1000, True, "Rising or Falling", False),
    ("x1", 0.5, 40, False, "Rising", True),
]


@pytest.mark.parametrize("probe,native_v,interval,trig_on,direction,mf_on", CASES)
def test_config_from_settings(settings_env, probe, native_v, interval, trig_on, direction, mf_on):
    seed = {
        "ratemeter/probe": probe,
        "ratemeter/voltage_range_v": native_v,
        "ratemeter/sample_interval_ns": interval,
        "ratemeter/window_duration_ms": 7.5,
        "ratemeter/coupling": "AC",
        "ratemeter/trigger_enabled": trig_on,
        "ratemeter/trigger_direction": direction,
        "ratemeter/rate_averaging_s": 20,
        "ratemeter/width_rel_height_idx": 1,
        "ratemeter/bandwidth_limit_enabled": True,
        "ratemeter/captures_per_batch": 4,
        "ratemeter/detection_mode": rp._DETECTION_MODE_MATCHED_FILTER if mf_on else rp._DETECTION_MODE_SIMPLE,
        "ratemeter/mf_scale_factors": "[0.75, 1.0, 2.0]",
        "ratemeter/mf_polarity": "Both",
        "ratemeter/mf_corr_threshold": 0.5,
        "ratemeter/bands": BANDS,
    }
    page = make_page(seed)
    cfg = page._build_config()
    assert cfg.probe_factor == rp.PROBE_FACTORS[probe]
    assert cfg.voltage_range_v == pytest.approx(native_v)
    assert cfg.sample_interval_ns == interval
    assert cfg.window_duration_ms == pytest.approx(7.5)
    assert cfg.coupling == "AC"
    assert cfg.rate_averaging_s == 20.0
    assert cfg.width_rel_height == 0.2
    assert cfg.bandwidth_limit_enabled is True
    assert [(b.low_mv, b.high_mv, b.transit_min_width_ns) for b in cfg.bands] == [
        (1.0, 3.0, 800.0), (3.0, 6.0, None), (6.0, 12.0, 400.0),
    ]

    mf = page._build_mf_config()
    assert mf.enabled is mf_on
    assert mf.polarity == "both"
    assert mf.correlation_threshold == pytest.approx(0.5)
    assert mf.scale_factors == [0.75, 1.0, 2.0]

    assert page._trigger_direction_value() == rp._TRIGGER_DIRECTIONS[direction]
    assert page.chk_trigger_enable.isChecked() is trig_on
    assert page.spin_captures_per_batch.value() == 4


def test_save_load_round_trip(settings_env):
    page = make_page({
        "ratemeter/probe": "x10",
        "ratemeter/voltage_range_v": 0.05,
        "ratemeter/bands": BANDS,
        "ratemeter/mf_scale_factors": "[0.75, 1.0, 2.0]",
    })
    page._save_settings()
    first = (page._build_config(), page._build_mf_config())

    page2 = rp.RatemeterPage(DAQChannels())  # reloads what page saved
    assert (page2._build_config(), page2._build_mf_config()) == first


def test_saved_key_set_is_stable(settings_env):
    page = make_page()
    s = page._settings
    s.clear()
    page._save_settings()
    s.sync()
    written = {k for k in s.allKeys() if not k.startswith("ratemeter/section_expanded_")}
    assert written == EXPECTED_KEYS


# ---------------------------------------------------------------------------
# Tiles, dock and switches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("interval", [10, 20, 40, 80, 200, 1000])
def test_derived_scope_values_match_config(settings_env, interval):
    page = make_page({"ratemeter/sample_interval_ns": interval, "ratemeter/window_duration_ms": 3.33})
    samples, rate_hz = page._derived_scope_values()
    assert samples == page._build_config().num_samples
    assert rate_hz == pytest.approx(1e9 / interval)


def test_tiles_show_loaded_values(settings_env):
    page = make_page({
        "ratemeter/probe": "x10", "ratemeter/voltage_range_v": 0.02,
        "ratemeter/coupling": "AC", "ratemeter/trigger_enabled": True,
        "ratemeter/trigger_threshold_mv": 6.0, "ratemeter/trigger_direction": "Falling",
        "ratemeter/captures_per_batch": 3, "ratemeter/bandwidth_limit_enabled": True,
    })
    assert "AC" in page._tiles["channel"].primary_text() and "x10" in page._tiles["channel"].primary_text()
    assert page._tiles["trigger"].primary_text() == "6 mV ↓"
    assert page._tiles["captures"].primary_text() == "3 / batch"
    assert page._tiles["scope"].primary_text() == "5 ms"


def test_steppers_drive_existing_controls(settings_env):
    page = make_page()
    page._step_window(+1)
    assert page.spin_window.value() == 10.0
    page._step_window(-1)
    page._step_window(-1)
    assert page.spin_window.value() == 2.0
    before = page.combo_range.currentIndex()
    page._step_range(+1)
    assert page.combo_range.currentIndex() == before + 1
    assert "±" in page._tiles["channel"].primary_text()
    page._tiles["captures"].step_up.emit()
    assert page.spin_captures_per_batch.value() == 2


def test_dock_opens_one_panel_and_marks_tile(settings_env):
    page = make_page()
    assert page.dock.current_key is None
    page._tiles["trigger"].clicked.emit()
    page._tiles["rates"].clicked.emit()
    assert page.dock.current_key == "rates"
    assert page._tiles["rates"].is_open() and not page._tiles["trigger"].is_open()
    page._tiles["rates"].clicked.emit()
    assert page.dock.current_key is None and not page._tiles["rates"].is_open()


def test_switches_follow_real_state_on_failure(settings_env):
    page = make_page()
    assert not page.sw_connect.isChecked() and not page.sw_run.isEnabled()

    def boom():
        raise RuntimeError("no scope")
    page._service.connect = boom
    page.sw_connect.setChecked(True)          # what a click does before the handler runs
    page._on_connect_switch(True)
    assert not page.sw_connect.isChecked()    # snapped back
    assert "Error" in page.lbl_status.text()

    page._service = FakeService()
    page._on_connect_switch(True)
    assert page.sw_connect.isChecked() and page.sw_run.isEnabled()

    page._on_daq_busy(True)
    assert not page.sw_run.isEnabled()
    page._on_daq_busy(False)
    assert page.sw_run.isEnabled()
