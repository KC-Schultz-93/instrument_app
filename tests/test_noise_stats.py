"""
test_noise_stats.py
--------------------
Unit tests for the noise-calibration pure functions. No hardware required.
Uses synthetic Gaussian noise (with and without slow drift) and a small
deterministic array for the crossing-rate brute-force check.

Run with:
    python tests/test_noise_stats.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np

from instrument_app.services.noise_stats import (
    crossing_rate_hz,
    peak_to_peak,
    rms,
    robust_sigma_mad,
    suggested_threshold,
)

_SI_NS = 200
_N = 20_000
_SIGMA_V = 0.0005  # 0.5 mV


def test_rms_and_mad_agree_on_clean_gaussian():
    """On pure Gaussian noise, rms() and robust_sigma_mad() should both be
    close to the true sigma and close to each other."""
    rng = np.random.default_rng(1)
    voltage = rng.normal(0.0, _SIGMA_V, _N)

    measured_rms = rms(voltage)
    measured_mad = robust_sigma_mad(voltage)

    assert abs(measured_rms - _SIGMA_V) / _SIGMA_V < 0.10, (
        f"rms={measured_rms:.6f} V far from true sigma={_SIGMA_V:.6f} V"
    )
    assert abs(measured_mad - _SIGMA_V) / _SIGMA_V < 0.10, (
        f"robust_sigma_mad={measured_mad:.6f} V far from true sigma={_SIGMA_V:.6f} V"
    )
    assert abs(measured_rms - measured_mad) / _SIGMA_V < 0.10, (
        f"rms={measured_rms:.6f} V and robust_sigma_mad={measured_mad:.6f} V disagree on clean noise"
    )
    print(
        f"  rms_and_mad_agree_on_clean_gaussian:  rms={measured_rms:.6f} V, "
        f"mad={measured_mad:.6f} V, true={_SIGMA_V:.6f} V  - PASS"
    )


def test_mad_resists_drift_rms_does_not():
    """A slow sinusoidal baseline drift should inflate plain rms() but leave
    robust_sigma_mad() close to the true noise sigma — the core claim behind
    preferring MAD for threshold suggestions (see docs/runt_trigger_plan.md)."""
    rng = np.random.default_rng(2)
    t = np.arange(_N, dtype=np.float64)
    # One-tenth of a cycle across the whole trace: slow enough that each of
    # robust_sigma_mad's blocks sees only a small, near-linear slice of it.
    drift = 0.015 * np.sin(2 * np.pi * t / (_N * 10))
    voltage = rng.normal(0.0, _SIGMA_V, _N) + drift

    measured_rms = rms(voltage)
    measured_mad = robust_sigma_mad(voltage)

    assert abs(measured_mad - _SIGMA_V) / _SIGMA_V < 0.25, (
        f"robust_sigma_mad={measured_mad:.6f} V should stay near true sigma "
        f"{_SIGMA_V:.6f} V despite drift"
    )
    assert measured_rms > 3 * measured_mad, (
        f"rms={measured_rms:.6f} V should be clearly inflated by drift relative to "
        f"robust_sigma_mad={measured_mad:.6f} V"
    )
    print(
        f"  mad_resists_drift_rms_does_not:  rms={measured_rms:.6f} V (inflated), "
        f"mad={measured_mad:.6f} V (near true {_SIGMA_V:.6f} V)  - PASS"
    )


def test_peak_to_peak_matches_known_extremes():
    """peak_to_peak() on a deterministic array with known min/max."""
    voltage = np.array([-0.003, 0.001, 0.004, -0.002, 0.0005])
    result = peak_to_peak(voltage)
    expected = 0.004 - (-0.003)
    assert abs(result - expected) < 1e-12, f"Expected {expected}, got {result}"
    print(f"  peak_to_peak_matches_known_extremes:  {result:.4f} V  - PASS")


def test_suggested_threshold_scales_with_k():
    sigma = 0.0004
    assert suggested_threshold(sigma, k=5.0) == 5.0 * sigma
    assert suggested_threshold(sigma, k=3.0) == 3.0 * sigma
    print("  suggested_threshold_scales_with_k:  k*sigma for k=3,5  - PASS")


def test_crossing_rate_matches_brute_force():
    """crossing_rate_hz() matches an independent brute-force count on a small
    deterministic array."""
    # Hand-built so |voltage| crosses 0.5 (rising) at indices 2 and 6.
    voltage = np.array([0.1, 0.2, 0.6, 0.55, 0.3, 0.1, 0.7, 0.6, 0.2])
    threshold = 0.5
    sample_interval_ns = 1_000_000  # 1 ms/sample -> trace is 9 ms long

    above = np.abs(voltage) >= threshold
    brute_force_crossings = sum(
        1 for i in range(1, len(voltage)) if above[i] and not above[i - 1]
    )
    assert brute_force_crossings == 2, f"Test setup error: expected 2, got {brute_force_crossings}"

    duration_s = len(voltage) * sample_interval_ns * 1e-9
    expected_rate = brute_force_crossings / duration_s

    result = crossing_rate_hz(voltage, threshold, sample_interval_ns)
    assert abs(result - expected_rate) < 1e-9, (
        f"crossing_rate_hz={result} != brute-force {expected_rate}"
    )
    print(f"  crossing_rate_matches_brute_force:  {result:.3f} Hz (2 crossings / {duration_s*1000:.0f} ms)  - PASS")


def test_crossing_rate_zero_on_quiet_trace():
    """No crossings above a high threshold on low-amplitude noise -> 0 Hz, no division errors."""
    rng = np.random.default_rng(3)
    voltage = rng.normal(0.0, _SIGMA_V, _N)
    result = crossing_rate_hz(voltage, threshold=10 * _SIGMA_V, sample_interval_ns=_SI_NS)
    assert result == 0.0, f"Expected 0 Hz for a threshold far above the noise, got {result}"
    print("  crossing_rate_zero_on_quiet_trace:  0.0 Hz  - PASS")


if __name__ == "__main__":
    print("noise_stats tests (no hardware required)")
    print("=" * 50)
    test_rms_and_mad_agree_on_clean_gaussian()
    test_mad_resists_drift_rms_does_not()
    test_peak_to_peak_matches_known_extremes()
    test_suggested_threshold_scales_with_k()
    test_crossing_rate_matches_brute_force()
    test_crossing_rate_zero_on_quiet_trace()
    print()
    print("All noise_stats tests passed.")
