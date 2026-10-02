"""
test_matched_filter.py
-----------------------
Unit tests for MatchedFilter bipolar-pulse detection. No hardware required.
Uses synthetic waveforms with injected bipolar (sine-cycle) pulses.

Run with:
    python Tests/test_matched_filter.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np

from instrument_app.services.daq_models import MatchedFilterConfig
from instrument_app.services.matched_filter import MatchedFilter
from instrument_app.services.signal_extractor import SignalExtractor


# ---------------------------------------------------------------------------
# Synthetic waveform generators
# ---------------------------------------------------------------------------

_SI_NS = 200          # 200 ns sample interval
_N = 3000             # 600 us trace -- well over 3x the 85 us default period
_PERIOD_US = 85.0


def make_bipolar_trace(
    n_samples: int,
    sample_interval_ns: int,
    period_us: float,
    amplitude_v: float,
    noise_v: float,
    event_times_us: list,
    polarities: list = None,
    seed: int = 42,
) -> tuple:
    """
    Generate a synthetic waveform containing bipolar pulses at specified times.

    Each pulse is a single-cycle sine wave (positive_first) or its negation
    (negative_first), additively overlaid on Gaussian noise. Returns
    (time_ns, voltage), already zero-mean (baseline-corrected).
    """
    rng = np.random.default_rng(seed)
    time_ns = np.arange(n_samples, dtype=np.float64) * sample_interval_ns
    voltage = rng.normal(0.0, noise_v, n_samples)

    if polarities is None:
        polarities = ["positive_first"] * len(event_times_us)

    n_cycle = max(2, int(period_us * 1_000 / sample_interval_ns))
    cycle = amplitude_v * np.sin(np.linspace(0, 2 * np.pi, n_cycle, endpoint=False))

    for t_us, polarity in zip(event_times_us, polarities):
        idx = int(t_us * 1_000 / sample_interval_ns)
        pulse = -cycle if polarity == "negative_first" else cycle
        end = min(n_samples, idx + n_cycle)
        voltage[idx:end] += pulse[: end - idx]

    return time_ns, voltage


def make_spike_trace(
    n_samples: int,
    sample_interval_ns: int,
    noise_v: float,
    spike_time_us: float,
    spike_amplitude_v: float,
    width_samples: int = 10,
    seed: int = 42,
) -> tuple:
    """One-sided Gaussian noise spike -- not a bipolar shape."""
    rng = np.random.default_rng(seed)
    time_ns = np.arange(n_samples, dtype=np.float64) * sample_interval_ns
    voltage = rng.normal(0.0, noise_v, n_samples)

    idx = int(spike_time_us * 1_000 / sample_interval_ns)
    gaussian = spike_amplitude_v * np.exp(
        -0.5 * ((np.arange(n_samples) - idx) / width_samples) ** 2
    )
    voltage += gaussian
    return time_ns, voltage


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_detects_clean_bipolar():
    """A single clean bipolar pulse with no noise should produce exactly one event."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PERIOD_US, amplitude_v=0.05, noise_v=0.0, event_times_us=[200.0],
    )
    mf = MatchedFilter(MatchedFilterConfig(), _SI_NS)
    events = mf.detect(voltage, time_ns)
    assert len(events) == 1, f"Expected 1 event, got {len(events)}"
    assert abs(events[0].time_us - 200.0) < _PERIOD_US, (
        f"Event time {events[0].time_us:.1f} us far from injected time 200.0 us"
    )
    print(f"  detects_clean_bipolar:  1 event at t={events[0].time_us:.1f} us  - PASS")


def test_detects_subthreshold_bipolar():
    """A bipolar pulse below the simple-threshold noise floor should still be found.

    A random noise trace can occasionally exceed a 3-sigma height threshold by
    chance anywhere in the trace, so this doesn't run the simple detector against
    a stochastic draw. Instead it directly confirms the injected amplitude sits
    below the noise-derived threshold SignalExtractor would use by default (the
    premise of a "subthreshold" signal), then confirms the matched filter still
    finds the real event by shape.
    """
    event_time_us = 200.0
    amplitude_v = 0.03
    noise_v = 0.02
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PERIOD_US, amplitude_v=amplitude_v, noise_v=noise_v, event_times_us=[event_time_us],
    )

    height_threshold_v = SignalExtractor().min_height_sigma * noise_v
    assert amplitude_v < height_threshold_v, (
        f"Test setup error: amplitude {amplitude_v} V is not below the simple "
        f"threshold {height_threshold_v} V"
    )

    mf = MatchedFilter(MatchedFilterConfig(), _SI_NS)
    events = mf.detect(voltage, time_ns)
    assert len(events) >= 1, "Matched filter failed to detect a subthreshold bipolar pulse"
    nearest = min(events, key=lambda e: abs(e.time_us - event_time_us))
    assert abs(nearest.time_us - event_time_us) < _PERIOD_US, (
        f"Nearest matched-filter event at {nearest.time_us:.1f} us is far from the "
        f"injected event at {event_time_us:.1f} us"
    )
    print(
        f"  detects_subthreshold_bipolar:  amplitude {amplitude_v} V < simple threshold "
        f"{height_threshold_v:.3f} V, matched_filter found it at t={nearest.time_us:.1f} us  - PASS"
    )


def test_rejects_noise_spike():
    """A one-sided noise spike (not bipolar) should not be detected."""
    time_ns, voltage = make_spike_trace(
        _N, _SI_NS, noise_v=0.005, spike_time_us=200.0, spike_amplitude_v=0.08,
    )
    mf = MatchedFilter(MatchedFilterConfig(), _SI_NS)
    events = mf.detect(voltage, time_ns)
    assert len(events) == 0, f"Expected 0 events for a non-bipolar spike, got {len(events)}"
    print("  rejects_noise_spike:  0 events  - PASS")


def test_period_validation_rejects_wrong_period():
    """A bipolar pulse with a period outside [period_min_us, period_max_us] is rejected."""
    wrong_period_us = 60.0
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, wrong_period_us, amplitude_v=0.05, noise_v=0.0, event_times_us=[200.0],
    )
    # Template still matches the injected shape (same period used for both), but the
    # measured zero-crossing period falls outside the default [78, 92] us validation window.
    mf = MatchedFilter(MatchedFilterConfig(period_us=wrong_period_us), _SI_NS)
    events = mf.detect(voltage, time_ns)
    assert len(events) == 0, f"Expected 0 events for out-of-window period, got {len(events)}"
    print("  period_validation_rejects_wrong_period:  0 events  - PASS")


def test_both_polarities():
    """polarity='both' should detect one event of each polarity in the same trace."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PERIOD_US, amplitude_v=0.05, noise_v=0.0,
        event_times_us=[150.0, 350.0],
        polarities=["positive_first", "negative_first"],
    )
    mf = MatchedFilter(MatchedFilterConfig(polarity="both"), _SI_NS)
    events = mf.detect(voltage, time_ns)
    assert len(events) == 2, f"Expected 2 events, got {len(events)}"
    polarities_found = {e.polarity for e in events}
    assert polarities_found == {"positive_first", "negative_first"}, (
        f"Expected both polarities represented, got {polarities_found}"
    )
    print(f"  both_polarities:  2 events, polarities={sorted(polarities_found)}  - PASS")


def test_amplitude_extraction_accuracy():
    """Detected amplitude magnitude should match the injected amplitude within 10% at low noise.

    amplitude_v reports whichever lobe (positive or negative) has the larger
    magnitude, so for a near-symmetric sine cycle either lobe may win by a
    hair depending on noise -- the check is on magnitude, not sign.
    """
    amplitude_v = 0.05
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PERIOD_US, amplitude_v=amplitude_v, noise_v=0.0005, event_times_us=[200.0],
    )
    mf = MatchedFilter(MatchedFilterConfig(), _SI_NS)
    events = mf.detect(voltage, time_ns)
    assert len(events) == 1, f"Expected 1 event, got {len(events)}"
    rel_error = abs(abs(events[0].amplitude_v) - amplitude_v) / amplitude_v
    assert rel_error < 0.10, (
        f"Amplitude {events[0].amplitude_v:.4f} V vs injected {amplitude_v:.4f} V "
        f"(error {rel_error:.1%}) exceeds 10% tolerance"
    )
    print(
        f"  amplitude_extraction_accuracy:  {events[0].amplitude_v:.4f} V "
        f"(injected {amplitude_v:.4f} V, error {rel_error:.1%})  - PASS"
    )


if __name__ == "__main__":
    print("MatchedFilter tests (no hardware required)")
    print("=" * 50)
    test_detects_clean_bipolar()
    test_detects_subthreshold_bipolar()
    test_rejects_noise_spike()
    test_period_validation_rejects_wrong_period()
    test_both_polarities()
    test_amplitude_extraction_accuracy()
    print()
    print("All MatchedFilter tests passed.")
