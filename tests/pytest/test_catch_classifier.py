"""
test_catch_classifier.py
-------------------------
Offline tests for services/catch_classifier.py. No Qt, no hardware.

The real-capture tests replay the first caught particle (tests/pytest/data/
catch_example_FIGURE_1.csv.gz, a PicoScope export). The synthetic cases build
non-catch waveforms from the measured noise character so we can check what the
classifier rejects until real non-catch captures are available.

Run with:
    python -m pytest tests/pytest/test_catch_classifier.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import numpy as np
import pytest
from scipy.signal import lfilter

from instrument_app.services.catch_classifier import (
    CatchClassifierConfig,
    classify_catch,
    trigger_was_met,
)
from instrument_app.services.catch_recorder import load_pico_csv

EXAMPLE = Path(__file__).parent / "data" / "catch_example_FIGURE_1.csv.gz"
DT_NS = 200.0
N = 50000
T = np.arange(N) * DT_NS / 1e3          # microseconds


# --------------------------------------------------------------------------
# synthetic waveform builders (A noise is strongly low-pass, like the real A)
# --------------------------------------------------------------------------

def _colored(rng, sigma, rho=0.982):
    x = lfilter([1.0], [1.0, -rho], rng.standard_normal(N + 2000))[2000:]
    return x / x.std() * sigma


def _tail(amp, tau_us, t0_us=2250.0):
    y = np.zeros(N)
    m = T > t0_us
    y[m] = amp * np.exp(-(T[m] - t0_us) / tau_us)
    return y


def _lobe(t0, width_us):
    x = (T - t0) / width_us
    return -x * np.exp(-0.5 * x * x) * np.sqrt(np.e)


def _noise(rng):
    return _colored(rng, 0.24) + _tail(20.0, 500.0), _colored(rng, 0.02) - _tail(0.8, 2500.0)


def _catch(rng, period_us=242.0, a_amp=7.0, b_amp=0.35, start_us=3000.0):
    ph = (T - start_us) % period_us
    a = np.zeros(N)
    for k in range(int((T[-1] - start_us) // period_us) + 1):
        a += _lobe(start_us + k * period_us + 0.3 * period_us, 14.0) * a_amp
    sq = ((ph > 0.25 * period_us) & (ph < 0.6 * period_us) & (T > start_us)).astype(float)
    b = b_amp * np.convolve(sq, np.ones(60) / 60, mode="same")
    an, bn = _noise(rng)
    return a + an, b + bn


def _classify(a, b, **cfg):
    return classify_catch(a, b, DT_NS, CatchClassifierConfig(**cfg))


# --------------------------------------------------------------------------
# the real captured particle
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def example():
    t_ms, a, b = load_pico_csv(EXAMPLE)
    return t_ms, a, b


def test_real_capture_is_a_catch(example):
    t_ms, a, b = example
    dt_ns = (t_ms[1] - t_ms[0]) * 1e6
    r = classify_catch(a, b, dt_ns)
    assert r.is_catch, r.reason
    assert r.period_us == pytest.approx(241.8, rel=0.02)
    assert 4000 < r.frequency_hz < 4300
    assert r.a_coherence > 0.6 and r.b_coherence > 0.4
    assert r.n_pos_lobes >= 1 and r.n_neg_lobes >= 1
    assert r.b_shape_ratio > 0.3        # B is square-like (a sine would be ~0)


@pytest.mark.parametrize("start_ms", [3.0, 3.3, 3.5, 4.0, 5.0])
def test_real_capture_not_sensitive_to_window_start(example, start_ms):
    t_ms, a, b = example
    r = classify_catch(a, b, 200.0, CatchClassifierConfig(analysis_start_ms=start_ms))
    assert r.is_catch, r.reason


def test_window_that_includes_the_mirror_transient_is_flagged(example):
    """If the analysis window is set too early, the overranged mirror spike is
    inside it. The classifier must say so rather than silently mis-decide."""
    _, a, b = example
    r = classify_catch(a, b, 200.0, CatchClassifierConfig(analysis_start_ms=1.0))
    assert not r.is_catch
    assert "overrange" in r.reason


def test_the_transient_only_part_of_the_capture_is_not_a_catch(example):
    """First 3.2 ms = trigger transient + mirror switch, no particle yet."""
    _, a, b = example
    n = int(3.2e3 / 0.2)
    r = classify_catch(a[:n], b[:n], 200.0, CatchClassifierConfig(analysis_start_ms=0.5))
    assert not r.is_catch


# --------------------------------------------------------------------------
# no fixed frequency window: any period with enough cycles is accepted
# --------------------------------------------------------------------------

@pytest.mark.parametrize("period_us", [60.0, 100.0, 242.0, 500.0, 700.0])
def test_catches_at_many_frequencies(period_us):
    rng = np.random.default_rng(1)
    r = _classify(*_catch(rng, period_us=period_us))
    assert r.is_catch, r.reason
    assert r.period_us == pytest.approx(period_us, rel=0.05)


def test_weak_catch_still_accepted():
    r = _classify(*_catch(np.random.default_rng(2), a_amp=2.0, b_amp=0.1))
    assert r.is_catch, r.reason


def test_too_few_cycles_is_not_a_catch_but_min_cycles_is_adjustable():
    rng = np.random.default_rng(3)
    a, b = _catch(rng, period_us=1700.0)           # only ~4 cycles in the window
    assert not _classify(a, b).is_catch
    assert _classify(a, b, min_cycles=3.0).is_catch


# --------------------------------------------------------------------------
# things that must NOT be kept
# --------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(25))
def test_noise_and_settling_tail_is_rejected(seed):
    a, b = _noise(np.random.default_rng(seed))
    assert not _classify(a, b).is_catch


@pytest.mark.parametrize("seed", range(10))
def test_single_pass_through_pulse_is_rejected(seed):
    rng = np.random.default_rng(seed)
    a, b = _noise(rng)
    a = a + 7.0 * _lobe(5000.0, 14.0)
    b = b + 0.35 * np.convolve(((T > 4900) & (T < 5100)).astype(float), np.ones(60) / 60, mode="same")
    assert not _classify(a, b).is_catch


def test_periodic_A_without_B_is_rejected():
    rng = np.random.default_rng(4)
    a, _ = _catch(rng)
    _, b = _noise(rng)
    r = _classify(a, b)
    assert not r.is_catch and "B" in r.reason


def test_periodic_B_without_A_is_rejected():
    rng = np.random.default_rng(5)
    _, b = _catch(rng)
    a, _ = _noise(rng)
    assert not _classify(a, b).is_catch


def test_slow_mains_pickup_on_both_is_rejected():
    rng = np.random.default_rng(6)
    a, b = _noise(rng)
    a = a + 3.0 * np.sin(2 * np.pi * T / 16667.0)
    b = b + 0.2 * np.sin(2 * np.pi * T / 16667.0)
    assert not _classify(a, b).is_catch


def test_sinusoidal_pickup_on_both_channels_is_rejected_by_square_wave_gate():
    rng = np.random.default_rng(7)
    a, b = _noise(rng)
    a = a + 3.0 * np.sin(2 * np.pi * T / 50.0)
    b = b + 0.2 * np.sin(2 * np.pi * T / 50.0)
    r = _classify(a, b)
    assert not r.is_catch and "square" in r.reason
    # the gate can be switched off
    assert _classify(a, b, b_shape_ratio_min=0.0).is_catch


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------

def test_few_overrange_samples_in_window_are_tolerated():
    a, b = _catch(np.random.default_rng(8))
    a = a.copy()
    a[20000:20100] = np.inf
    b = b.copy()
    b[30000:30050] = -np.inf
    assert _classify(a, b).is_catch


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        classify_catch(np.zeros(100), np.zeros(99), DT_NS)


def test_short_window_is_rejected_not_crashed():
    r = classify_catch(np.zeros(300), np.zeros(300), DT_NS, CatchClassifierConfig(analysis_start_ms=0.0))
    assert not r.is_catch and "too short" in r.reason


def test_result_serialises_to_plain_dict():
    r = _classify(*_catch(np.random.default_rng(9)))
    d = r.to_dict()
    assert d["is_catch"] is True and isinstance(d["frequency_hz"], float)


# --------------------------------------------------------------------------
# trigger validity (separates real triggers from auto-trigger timeouts)
# --------------------------------------------------------------------------

def test_trigger_met_rising():
    a = np.zeros(100)
    a[3] = 0.007
    assert trigger_was_met(a, True, 0.006, "RISING")
    assert not trigger_was_met(np.zeros(100), True, 0.006, "RISING")


def test_trigger_met_falling_and_either():
    a = np.zeros(100)
    a[2] = -0.007
    assert trigger_was_met(a, True, -0.006, "FALLING")
    assert trigger_was_met(a, True, -0.006, "RISING_OR_FALLING")
    assert not trigger_was_met(np.zeros(100), True, -0.006, "FALLING")


def test_trigger_disabled_counts_everything():
    assert trigger_was_met(np.zeros(100), False, 0.006, "RISING")


def test_trigger_unknown_when_start_is_overrange():
    a = np.full(100, np.nan)
    assert trigger_was_met(a, True, 0.006, "RISING")
