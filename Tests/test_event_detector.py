"""
test_event_detector.py
----------------------
Unit tests for EventDetector, SignalExtractor, and their interaction.
No hardware required -- uses synthetic waveforms.

Run with:
    python Tests/test_event_detector.py

Synthetic trace notes:
  - n=3000 samples, sample_interval_ns=100 -> 300 us trace
  - noise_v=0.0002 V (0.2 mV) ensures tails of Gaussian peaks stay below
    the 3-sigma detection threshold (~0.010 + 3*0.0002 = 0.0106 V), giving
    clean separation between physical peaks and noise floor.
  - Peaks are spaced >=1000 samples apart so min_distance_samples=50 never
    causes adjacent peaks to be merged.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from datetime import datetime

from Services.DAQModels import AcquisitionConfig, WaveformRecord
from Services.EventDetector import (
    EventDetector,
    CLASS_EMPTY,
    CLASS_NOISY,
    CLASS_CLIPPED,
    CLASS_POSSIBLE,
    CLASS_LIKELY_ION,
    CLASS_AMBIGUOUS,
)
from Services.SignalExtractor import SignalExtractor
from Services.WaveformProcessor import WaveformProcessor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Trace parameters used across most tests.
_SI_NS = 100          # sample interval (ns)
_N = 3000             # samples per trace
_BASELINE = 0.010     # V
_NOISE = 0.0002       # V  (very low noise ensures clean peak separation)
_AMPLITUDE = 0.1      # V  (peak amplitude, ~500x above threshold)
_WIDTH = 50           # Gaussian sigma in samples

_DEFAULT_CONFIG = AcquisitionConfig(
    channel="A",
    voltage_range_v=2.0,
    coupling="DC",
    sample_interval_ns=_SI_NS,
    num_samples=_N,
)


def make_synthetic_trace(
    n: int = _N,
    baseline_v: float = _BASELINE,
    noise_v: float = _NOISE,
    peak_times_ns: list = None,
    peak_amplitude_v: float = _AMPLITUDE,
    sample_interval_ns: int = _SI_NS,
    seed: int = 42,
) -> tuple:
    """
    Return (time_ns, voltage) arrays.

    Gaussian peaks (sigma=_WIDTH samples) are injected at the specified times.
    The low default noise ensures peaks are unambiguously detectable.
    """
    rng = np.random.default_rng(seed)
    time_ns = np.arange(n, dtype=np.float64) * sample_interval_ns
    voltage = rng.normal(baseline_v, noise_v, n)
    if peak_times_ns:
        for pt_ns in peak_times_ns:
            idx = int(pt_ns / sample_interval_ns)
            gaussian = peak_amplitude_v * np.exp(
                -0.5 * ((np.arange(n) - idx) / _WIDTH) ** 2
            )
            voltage += gaussian
    return time_ns, voltage


def make_record(voltage, time_ns, config=None):
    cfg = config or _DEFAULT_CONFIG
    return WaveformRecord(
        trace_id=1,
        run_id="test",
        timestamp=datetime.now(),
        voltage=voltage,
        time_ns=time_ns,
        sample_interval_ns=cfg.sample_interval_ns,
        config=cfg,
    )


def run_pipeline(voltage, time_ns, config=None):
    """Baseline -> peaks -> classify. Returns (label, accepted, peaks, stats)."""
    extractor = SignalExtractor()
    detector = EventDetector()

    baseline_mean, baseline_rms = WaveformProcessor.estimate_baseline(voltage)
    corrected = WaveformProcessor.subtract_baseline(voltage, baseline_mean)
    peaks = extractor.find_peaks(corrected, baseline_mean, baseline_rms, time_ns)
    record = make_record(voltage, time_ns, config)
    label, accepted = detector.classify(record, peaks, baseline_mean, baseline_rms)
    stats = extractor.summarize(peaks, baseline_mean, baseline_rms)
    return label, accepted, peaks, stats


# ---------------------------------------------------------------------------
# EventDetector tests
# ---------------------------------------------------------------------------

def test_empty_trace():
    """A noise-only trace (no peaks) should be classified as empty."""
    time_ns, v = make_synthetic_trace(peak_times_ns=[])
    label, accepted, peaks, _ = run_pipeline(v, time_ns)
    assert label == CLASS_EMPTY, f"Expected {CLASS_EMPTY}, got {label}"
    assert not accepted
    print(f"  empty trace  ->  {label}  accepted={accepted}  - PASS")


def test_single_peak_trace():
    """A trace with one clear peak should be 'possible_event'."""
    # Peak at sample 1500 (middle of trace); no others nearby
    time_ns, v = make_synthetic_trace(peak_times_ns=[150_000])
    label, accepted, peaks, _ = run_pipeline(v, time_ns)
    assert label in (CLASS_POSSIBLE, CLASS_LIKELY_ION), \
        f"Expected possible/likely_ion, got {label}"
    assert accepted
    assert len(peaks) >= 1
    print(f"  single peak  ->  {label}  peaks={len(peaks)}  - PASS")


def test_two_periodic_peaks():
    """Two regularly-spaced peaks should be 'likely_trapped_ion'."""
    # Peaks at samples 750 and 1750 (1000 samples = 100 us apart)
    time_ns, v = make_synthetic_trace(peak_times_ns=[75_000, 175_000])
    label, accepted, peaks, stats = run_pipeline(v, time_ns)
    assert label == CLASS_LIKELY_ION, f"Expected {CLASS_LIKELY_ION}, got {label}"
    assert accepted
    assert len(peaks) >= 2
    print(f"  two periodic peaks  ->  {label}  peaks={len(peaks)}"
          f"  spacing={stats['mean_peak_spacing_ns']/1e3:.1f} us  - PASS")


def test_three_periodic_peaks():
    """Three regularly-spaced peaks should also be 'likely_trapped_ion'."""
    # Peaks at samples 500, 1500, 2500 (1000 samples = 100 us apart)
    time_ns, v = make_synthetic_trace(peak_times_ns=[50_000, 150_000, 250_000])
    label, accepted, peaks, stats = run_pipeline(v, time_ns)
    assert label == CLASS_LIKELY_ION, f"Expected {CLASS_LIKELY_ION}, got {label}"
    assert accepted
    assert len(peaks) >= 3
    spacing_us = stats['mean_peak_spacing_ns'] / 1e3
    print(f"  three periodic peaks  ->  {label}  peaks={len(peaks)}"
          f"  spacing={spacing_us:.1f} us  - PASS")


def test_clipped_trace():
    """A trace with saturated samples should be classified as overload."""
    time_ns, v = make_synthetic_trace(peak_times_ns=[])
    v_clipped = v.copy()
    v_clipped[1500] = 1.95   # exceeds 0.95 * 2.0 V
    label, accepted, _, _ = run_pipeline(v_clipped, time_ns)
    assert label == CLASS_CLIPPED, f"Expected {CLASS_CLIPPED}, got {label}"
    assert not accepted
    print(f"  clipped trace  ->  {label}  - PASS")


def test_noisy_trace():
    """A trace with very high baseline noise should be classified as noisy."""
    time_ns, v = make_synthetic_trace(
        noise_v=0.010,           # 10 mV noise - well above noise_threshold_v=5 mV
        peak_times_ns=[150_000], # has a peak, but noise is too high
        peak_amplitude_v=0.05,
    )
    label, accepted, _, _ = run_pipeline(v, time_ns)
    # noisy or empty depending on whether signal exceeds 3-sigma threshold
    assert label in (CLASS_NOISY, CLASS_EMPTY), \
        f"Expected noisy or empty, got {label}"
    assert not accepted
    print(f"  noisy trace  ->  {label}  - PASS")


# ---------------------------------------------------------------------------
# SignalExtractor tests
# ---------------------------------------------------------------------------

def test_peak_spacing_regular():
    """Peak spacings for regularly-spaced peaks should have low variation."""
    extractor = SignalExtractor()
    time_ns, v = make_synthetic_trace(peak_times_ns=[50_000, 150_000, 250_000])
    baseline_mean, baseline_rms = WaveformProcessor.estimate_baseline(v)
    corrected = WaveformProcessor.subtract_baseline(v, baseline_mean)
    peaks = extractor.find_peaks(corrected, baseline_mean, baseline_rms, time_ns)
    spacings = extractor.peak_spacings(peaks)
    assert spacings is not None and len(spacings) > 0, \
        f"Expected spacings, got None (peaks found: {len(peaks)})"
    cv = extractor.spacing_variation(spacings)
    print(f"  spacing CV={cv:.3f} (expected < 0.15)  - ", end="")
    assert cv < 0.15, f"Spacing variation too high: {cv:.3f}"
    print("PASS")


def test_summarize_no_peaks():
    """summarize() on an empty peaks list should return num_peaks=0 and None fields."""
    extractor = SignalExtractor()
    stats = extractor.summarize([], baseline_mean=0.0, baseline_rms=0.001)
    assert stats["num_peaks"] == 0
    assert stats["mean_peak_height_v"] is None
    assert stats["mean_peak_spacing_ns"] is None
    print("  summarize (no peaks):  PASS")


if __name__ == "__main__":
    print("EventDetector & SignalExtractor tests (no hardware required)")
    print("=" * 60)
    test_empty_trace()
    test_single_peak_trace()
    test_two_periodic_peaks()
    test_three_periodic_peaks()
    test_clipped_trace()
    test_noisy_trace()
    test_peak_spacing_regular()
    test_summarize_no_peaks()
    print()
    print("All EventDetector tests passed.")
