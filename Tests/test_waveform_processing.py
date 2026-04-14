"""
test_waveform_processing.py
---------------------------
Unit tests for WaveformProcessor. No hardware required.
Uses synthetic waveforms with injected Gaussian peaks.

Run with:
    python Tests/test_waveform_processing.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from Services.WaveformProcessor import WaveformProcessor


# ---------------------------------------------------------------------------
# Synthetic waveform generator
# ---------------------------------------------------------------------------

def make_synthetic_trace(
    n: int = 1000,
    baseline_v: float = 0.020,
    noise_v: float = 0.001,
    peak_times_ns: list = None,
    peak_amplitude_v: float = 0.1,
    sample_interval_ns: int = 100,
    seed: int = 42,
) -> tuple:
    """
    Returns (time_ns, voltage) arrays.

    Gaussian peaks are injected at the requested times (nanoseconds from trace
    start) with a fixed width of 20 samples.
    """
    rng = np.random.default_rng(seed)
    time_ns = np.arange(n, dtype=np.float64) * sample_interval_ns
    voltage = rng.normal(baseline_v, noise_v, n)

    if peak_times_ns:
        for pt_ns in peak_times_ns:
            idx = int(pt_ns / sample_interval_ns)
            width = 50
            gaussian = peak_amplitude_v * np.exp(
                -0.5 * ((np.arange(n) - idx) / width) ** 2
            )
            voltage += gaussian

    return time_ns, voltage


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_estimate_baseline_clean():
    """Baseline of a noise-only trace should be close to the true mean."""
    _, v = make_synthetic_trace(peak_times_ns=[])
    mean, rms = WaveformProcessor.estimate_baseline(v, fraction=0.1)
    assert abs(mean - 0.020) < 0.005, f"Baseline mean wrong: {mean:.5f}"
    assert 0 < rms < 0.005, f"RMS out of range: {rms:.6f}"
    print(f"  baseline (clean):  mean={mean:.4f} V  rms={rms*1e6:.1f} uV  - PASS")


def test_estimate_baseline_with_signal():
    """Baseline estimated from first 10% should not be contaminated by mid-trace peaks."""
    _, v = make_synthetic_trace(
        n=1000, peak_times_ns=[500_000, 800_000],  # peaks at sample 500 & 800
        sample_interval_ns=1000,
    )
    mean, rms = WaveformProcessor.estimate_baseline(v, fraction=0.1)
    # Only first 100 samples used - peaks are at 500 & 800
    assert abs(mean - 0.020) < 0.005, f"Baseline mean contaminated: {mean:.5f}"
    print(f"  baseline (with peaks):  mean={mean:.4f} V  - PASS")


def test_subtract_baseline():
    """After subtraction, the baseline region mean should be near zero."""
    _, v = make_synthetic_trace()
    mean, _ = WaveformProcessor.estimate_baseline(v)
    corrected = WaveformProcessor.subtract_baseline(v, mean)
    residual_mean = float(corrected[:100].mean())
    assert abs(residual_mean) < 1e-3, f"Residual mean too large: {residual_mean}"
    print(f"  subtract_baseline:  residual_mean={residual_mean:.2e} V  - PASS")


def test_invert():
    """Inversion should negate all values."""
    _, v = make_synthetic_trace()
    inv = WaveformProcessor.invert(v)
    assert np.allclose(inv, -v)
    print("  invert:  PASS")


def test_smooth_reduces_noise():
    """A heavily smoothed trace should have lower peak-to-peak variation."""
    _, v = make_synthetic_trace()
    smoothed = WaveformProcessor.smooth(v, window=51)
    assert smoothed.std() < v.std(), "Smoothed trace not smoother than original"
    print(f"  smooth:  std {v.std():.5f} -> {smoothed.std():.5f}  - PASS")


def test_clipping_detected():
    """A sample pushed above 95% of range should be flagged as clipped."""
    _, v = make_synthetic_trace()
    v_clipped = v.copy()
    v_clipped[100] = 1.95  # just above 0.95 * 2.0 V
    assert WaveformProcessor.is_clipped(v_clipped, voltage_range_v=2.0)
    print("  is_clipped (positive):  PASS")


def test_no_clipping_normal():
    """A normal trace should not be flagged as clipped."""
    _, v = make_synthetic_trace()
    # v is ~0.02 V with 0.001 V noise - well below ±2 V
    assert not WaveformProcessor.is_clipped(v, voltage_range_v=2.0)
    print("  is_clipped (negative):  PASS")


def test_noise_rms():
    """noise_rms on a flat trace should match the injected noise level."""
    _, v = make_synthetic_trace(noise_v=0.002, peak_times_ns=[])
    mean, _ = WaveformProcessor.estimate_baseline(v)
    rms = WaveformProcessor.noise_rms(v, mean)
    assert abs(rms - 0.002) < 0.001, f"Noise RMS wrong: {rms:.5f}"
    print(f"  noise_rms:  {rms*1e3:.2f} mV  (expected ~2.0 mV)  - PASS")


def test_short_trace_raises():
    """estimate_baseline should raise ValueError for very short traces."""
    v = np.array([0.1, 0.2])
    try:
        WaveformProcessor.estimate_baseline(v, fraction=0.1)
        assert False, "Should have raised ValueError"
    except ValueError:
        print("  short trace ValueError:  PASS")


if __name__ == "__main__":
    print("WaveformProcessor tests (no hardware required)")
    print("=" * 50)
    test_estimate_baseline_clean()
    test_estimate_baseline_with_signal()
    test_subtract_baseline()
    test_invert()
    test_smooth_reduces_noise()
    test_clipping_detected()
    test_no_clipping_normal()
    test_noise_rms()
    test_short_trace_raises()
    print()
    print("All WaveformProcessor tests passed.")
