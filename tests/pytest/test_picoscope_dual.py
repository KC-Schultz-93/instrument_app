"""
test_picoscope_dual.py
-----------------------
PicoScopeService.configure_channels_dual / run_block_dual against a fake ps4000
driver (no hardware): channel set-up, A/B buffer mapping, ADC -> volts with each
channel's own range and probe, overflow bits, abort while waiting for a trigger.

This checks our calling logic, not the real driver; the first run on the real
PicoScope 4262 is still the true test.

Run with:
    python -m pytest tests/pytest/test_picoscope_dual.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))


import numpy as np
import pytest

from instrument_app.services.daq_models import AcquisitionConfig
from instrument_app.services.picoscope_service import PicoScopeService, _ADC_MAX

N = 64


class FakePs:
    """Minimal ps4000 stand-in that records calls and fills buffers on GetValues."""

    def __init__(self, ready_after=2, overflow=0):
        self.calls = []
        self._ready_after = ready_after
        self._polls = 0
        self._buffers = {}
        self._overflow = overflow
        self.counts = {0: np.arange(N, dtype=np.int16) * 100,        # channel A raw counts
                       1: -np.arange(N, dtype=np.int16) * 50}        # channel B raw counts

    def ps4000SetChannel(self, handle, ch, enabled, coupling, rng):
        self.calls.append(("SetChannel", ch, enabled, coupling, rng))
        return 0

    def ps4000SetBwFilter(self, handle, ch, enabled):
        self.calls.append(("SetBwFilter", ch, enabled))
        return 0

    def ps4000Stop(self, handle):
        self.calls.append(("Stop",))
        return 0

    def ps4000RunBlock(self, handle, pre, post, timebase, oversample, indisposed, seg, cb, param):
        self.calls.append(("RunBlock", pre, post, timebase))
        self._polls = 0
        return 0

    def ps4000IsReady(self, handle, ready_ref):
        self._polls += 1
        ready_ref._obj.value = 1 if self._polls > self._ready_after else 0
        return 0

    def ps4000SetDataBuffers(self, handle, ch, buf_ref, buf_min_ref, n):
        self._buffers[ch] = buf_ref._obj
        self.calls.append(("SetDataBuffers", ch, n))
        return 0

    def ps4000GetValues(self, handle, start, n_ref, ratio, mode, seg, overflow_ref):
        for ch, buf in self._buffers.items():
            buf[:] = self.counts[ch].tolist()
        n_ref._obj.value = N
        overflow_ref._obj.value = self._overflow
        self.calls.append(("GetValues",))
        return 0


def _service(ps):
    svc = PicoScopeService()
    svc._ps = ps
    return svc


def _cfgs(**b_kw):
    a = AcquisitionConfig("A", 0.02, "DC", 200, N, True, 0.005, "RISING", probe_factor=1.0)
    b = AcquisitionConfig("B", 0.05, "AC", 200, N, probe_factor=10.0, bandwidth_limit_enabled=True, **b_kw)
    return a, b


def test_configure_enables_both_channels_with_their_own_settings():
    ps = FakePs()
    a, b = _cfgs()
    _service(ps).configure_channels_dual(a, b)
    set_ch = [c for c in ps.calls if c[0] == "SetChannel"]
    assert [c[1] for c in set_ch] == [0, 1] and all(c[2] == 1 for c in set_ch)
    assert set_ch[0][3] == 1 and set_ch[1][3] == 0          # A DC, B AC
    assert set_ch[0][4] == 1 and set_ch[1][4] == 2          # 20 mV, 50 mV range enums
    bw = {c[1]: c[2] for c in ps.calls if c[0] == "SetBwFilter"}
    assert bw == {0: 0, 1: 1}


def test_run_block_dual_maps_buffers_and_scales_each_channel():
    ps = FakePs(overflow=0b10)           # channel B overranged
    a, b = _cfgs()
    rec = _service(ps).run_block_dual(a, b)

    exp_a = ps.counts[0].astype(float) / _ADC_MAX * 0.02 * 1.0
    exp_b = ps.counts[1].astype(float) / _ADC_MAX * 0.05 * 10.0     # B's own range and probe
    np.testing.assert_allclose(rec.voltage_a, exp_a)
    np.testing.assert_allclose(rec.voltage_b, exp_b)
    assert rec.metadata["overflow_a"] is False and rec.metadata["overflow_b"] is True
    assert rec.sample_interval_ns == 200 and rec.voltage_a.size == N
    np.testing.assert_allclose(rec.time_ns, np.arange(N) * 200.0)
    assert rec.config_a is a and rec.config_b is b
    assert {c[1] for c in ps.calls if c[0] == "SetDataBuffers"} == {0, 1}


def test_polarity_inversion_applies_per_channel():
    ps = FakePs()
    a, b = _cfgs()
    b.invert_polarity = True
    rec = _service(ps).run_block_dual(a, b)
    exp_b = -(ps.counts[1].astype(float) / _ADC_MAX * 0.05 * 10.0)
    np.testing.assert_allclose(rec.voltage_b, exp_b)
    assert rec.voltage_a[1] > 0


def test_pre_trigger_and_post_trigger_split():
    ps = FakePs()
    a, b = _cfgs()
    a.pre_trigger_samples = b.pre_trigger_samples = 10
    _service(ps).run_block_dual(a, b)
    run = next(c for c in ps.calls if c[0] == "RunBlock")
    assert run[1] == 10 and run[2] == N - 10


def test_abort_while_waiting_for_trigger_cancels_and_returns_none():
    ps = FakePs(ready_after=10_000)       # never ready
    a, b = _cfgs()
    seen = {"n": 0}

    def abort():
        seen["n"] += 1
        return seen["n"] > 3

    assert _service(ps).run_block_dual(a, b, abort_check=abort) is None
    assert ("Stop",) in ps.calls[-2:] or ps.calls[-1] == ("Stop",)
    assert not any(c[0] == "GetValues" for c in ps.calls)


def test_waiting_without_abort_check_is_capped(monkeypatch):
    ps = FakePs(ready_after=10 ** 9)
    a, b = _cfgs()
    monkeypatch.setattr("instrument_app.services.picoscope_service.time.sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="timed out"):
        _service(ps).run_block_dual(a, b)


@pytest.mark.parametrize("mutate, message", [
    (lambda a, b: setattr(a, "channel", "B"), "channel A"),
    (lambda a, b: setattr(b, "channel", "A"), "channel A"),
    (lambda a, b: setattr(b, "sample_interval_ns", 400), "share"),
    (lambda a, b: setattr(b, "num_samples", N + 1), "share"),
])
def test_mismatched_configs_are_rejected(mutate, message):
    a, b = _cfgs()
    mutate(a, b)
    with pytest.raises(ValueError, match=message):
        _service(FakePs()).run_block_dual(a, b)
    with pytest.raises(ValueError):
        _service(FakePs()).configure_channels_dual(a, b)


def test_existing_single_channel_path_is_untouched():
    """configure_channel (ratemeter / DAQ) still disables the other channel."""
    ps = FakePs()
    cfg = AcquisitionConfig("A", 0.02, "DC", 200, N)
    _service(ps).configure_channel(cfg)
    enabled = {c[1]: c[2] for c in ps.calls if c[0] == "SetChannel"}
    assert enabled == {0: 1, 1: 0}
