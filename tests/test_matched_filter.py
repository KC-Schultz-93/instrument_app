"""
test_matched_filter.py
-----------------------
Unit tests for MatchedFilter bipolar-pulse detection. No hardware required.
Uses synthetic waveforms with injected bipolar (sine-cycle) pulses.

"Pulse duration" is the peak-to-peak time of a pulse, which is half the period
of the single sine cycle that makes up the pulse.

Run with:
    python tests/test_matched_filter.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np

from instrument_app.services.daq_models import MatchedFilterConfig
from instrument_app.services.matched_filter import MatchedFilter, _peak_to_peak_samples
from instrument_app.services.signal_extractor import SignalExtractor

_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "template.npy"


# ---------------------------------------------------------------------------
# Synthetic waveform generators
# ---------------------------------------------------------------------------

_SI_NS = 200          # 200 ns sample interval
_N = 8000             # 1600 us trace -- room for pulses up to ~200 us long
_PULSE_US = 85.0      # nominal peak-to-peak pulse duration


def make_bipolar_trace(
    n_samples: int,
    sample_interval_ns: int,
    pulse_duration_us: float,
    amplitude_v: float,
    noise_v: float,
    event_times_us: list,
    polarities: list = None,
    seed: int = 42,
) -> tuple:
    """
    Generate a synthetic waveform containing bipolar pulses at specified times.

    Each pulse is a single-cycle sine wave (positive_first) or its negation
    (negative_first) whose peak-to-peak time is pulse_duration_us, additively
    overlaid on Gaussian noise. Returns (time_ns, voltage), already zero-mean
    (baseline-corrected).
    """
    rng = np.random.default_rng(seed)
    time_ns = np.arange(n_samples, dtype=np.float64) * sample_interval_ns
    voltage = rng.normal(0.0, noise_v, n_samples)

    if polarities is None:
        polarities = ["positive_first"] * len(event_times_us)

    n_cycle = max(2, int(2 * pulse_duration_us * 1_000 / sample_interval_ns))
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
# Single-scale tests
# ---------------------------------------------------------------------------

def test_detects_clean_bipolar():
    """A single clean bipolar pulse with no noise should produce exactly one event."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PULSE_US, amplitude_v=0.05, noise_v=0.0, event_times_us=[400.0],
    )
    mf = MatchedFilter(MatchedFilterConfig(), _SI_NS)
    events = mf.detect(voltage, time_ns)
    assert len(events) == 1, f"Expected 1 event, got {len(events)}"
    assert abs(events[0].time_us - 400.0) < 2 * _PULSE_US, (
        f"Event time {events[0].time_us:.1f} us far from injected time 400.0 us"
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
    event_time_us = 400.0
    amplitude_v = 0.03
    noise_v = 0.02
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PULSE_US, amplitude_v=amplitude_v, noise_v=noise_v, event_times_us=[event_time_us],
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
    assert abs(nearest.time_us - event_time_us) < 2 * _PULSE_US, (
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
        _N, _SI_NS, noise_v=0.005, spike_time_us=400.0, spike_amplitude_v=0.08,
    )
    mf = MatchedFilter(MatchedFilterConfig(), _SI_NS)
    events = mf.detect(voltage, time_ns)
    assert len(events) == 0, f"Expected 0 events for a non-bipolar spike, got {len(events)}"
    print("  rejects_noise_spike:  0 events  - PASS")


def test_duration_validation_rejects_wrong_duration():
    """A bipolar pulse with a duration outside [pulse_min_us, pulse_max_us] is rejected."""
    wrong_duration_us = 45.0
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, wrong_duration_us, amplitude_v=0.05, noise_v=0.0, event_times_us=[400.0],
    )
    # Template matches the injected shape (same duration used for both), but the
    # measured duration falls below the default [60, 110] us validation window.
    mf = MatchedFilter(MatchedFilterConfig(pulse_duration_us=wrong_duration_us), _SI_NS)
    events = mf.detect(voltage, time_ns)
    assert len(events) == 0, f"Expected 0 events for out-of-window duration, got {len(events)}"
    print("  duration_validation_rejects_wrong_duration:  0 events  - PASS")


def test_both_polarities():
    """polarity='both' should detect one event of each polarity in the same trace."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PULSE_US, amplitude_v=0.05, noise_v=0.0,
        event_times_us=[400.0, 900.0],
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
        _N, _SI_NS, _PULSE_US, amplitude_v=amplitude_v, noise_v=0.0005, event_times_us=[400.0],
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


# ---------------------------------------------------------------------------
# Multi-scale tests
# ---------------------------------------------------------------------------

def test_scale_1_0_matches_baseline():
    """scale_factors=[1.0] gives the same result as the default (no scale) config."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PULSE_US, amplitude_v=0.05, noise_v=0.0005, event_times_us=[400.0],
    )
    default_events = MatchedFilter(MatchedFilterConfig(), _SI_NS).detect(voltage, time_ns)
    scaled_events = MatchedFilter(MatchedFilterConfig(scale_factors=[1.0]), _SI_NS).detect(voltage, time_ns)
    assert len(default_events) == len(scaled_events) == 1
    assert default_events[0].event_index == scaled_events[0].event_index
    assert default_events[0].correlation_score == scaled_events[0].correlation_score
    print("  scale_1_0_matches_baseline:  identical to default config  - PASS")


def test_scale_2_0_detects_stretched_pulse():
    """A pulse at 2x the nominal duration is missed at scale 1.0 but found at scale 2.0."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, 2 * _PULSE_US, amplitude_v=0.05, noise_v=0.0, event_times_us=[500.0],
    )
    missed = MatchedFilter(MatchedFilterConfig(scale_factors=[1.0]), _SI_NS).detect(voltage, time_ns)
    found = MatchedFilter(MatchedFilterConfig(scale_factors=[1.0, 2.0]), _SI_NS).detect(voltage, time_ns)
    assert len(missed) == 0, f"Scale 1.0 should not accept a 2x-long pulse, got {len(missed)}"
    assert len(found) == 1, f"Scale 2.0 should find the stretched pulse, got {len(found)}"
    print("  scale_2_0_detects_stretched_pulse:  missed at 1.0, found at 2.0  - PASS")


def test_deduplication_keeps_best_score():
    """Two scales that both fire on one event (within min_distance_us) yield a single hit."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PULSE_US, amplitude_v=0.05, noise_v=0.0, event_times_us=[400.0],
    )
    # 1.0 and 1.1 are close enough that both templates correlate strongly and pass
    # their (scaled) duration gates on the same 85 us pulse.
    events = MatchedFilter(MatchedFilterConfig(scale_factors=[1.0, 1.1]), _SI_NS).detect(voltage, time_ns)
    assert len(events) == 1, f"Expected 1 deduplicated event, got {len(events)}"

    best_alone = max(
        MatchedFilter(MatchedFilterConfig(scale_factors=[s]), _SI_NS).detect(voltage, time_ns)[0].correlation_score
        for s in (1.0, 1.1)
    )
    assert events[0].correlation_score == best_alone, "Dedup did not keep the higher-scoring hit"
    print(f"  deduplication_keeps_best_score:  1 event, score={events[0].correlation_score:.3f}  - PASS")


def test_matched_scale_recorded():
    """BipolarEventRecord.matched_scale reflects the scale that produced the hit."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, 2 * _PULSE_US, amplitude_v=0.05, noise_v=0.0, event_times_us=[500.0],
    )
    events = MatchedFilter(MatchedFilterConfig(scale_factors=[1.0, 2.0]), _SI_NS).detect(voltage, time_ns)
    assert len(events) == 1
    assert events[0].matched_scale == 2.0, f"Expected matched_scale 2.0, got {events[0].matched_scale}"
    print("  matched_scale_recorded:  matched_scale=2.0  - PASS")


def test_empty_scale_list_raises():
    """scale_factors=[] (or a non-positive value) raises ValueError before any detection runs."""
    for bad in ([], [1.0, 0.0], [-1.0]):
        try:
            MatchedFilter(MatchedFilterConfig(scale_factors=bad), _SI_NS)
        except ValueError:
            continue
        raise AssertionError(f"scale_factors={bad} should raise ValueError")
    print("  empty_scale_list_raises:  ValueError  - PASS")


# ---------------------------------------------------------------------------
# Empirical template test
# ---------------------------------------------------------------------------

def test_empirical_template_resampled_to_acquisition_interval():
    """The .npy template is resampled so its peak-to-peak spans pulse_duration_us at any sample interval."""
    assert _TEMPLATE_PATH.exists(), f"Missing example template: {_TEMPLATE_PATH}"
    for si_ns in (100, 200, 400):
        cfg = MatchedFilterConfig(
            use_empirical_template=True,
            empirical_template_path=str(_TEMPLATE_PATH),
            pulse_duration_us=_PULSE_US,
        )
        mf = MatchedFilter(cfg, si_ns)
        template = mf._templates[0][1][0][1]
        p2p_us = _peak_to_peak_samples(template) * si_ns / 1_000
        assert abs(p2p_us - _PULSE_US) / _PULSE_US < 0.02, (
            f"At {si_ns} ns, template peak-to-peak {p2p_us:.1f} us != {_PULSE_US} us"
        )
    print("  empirical_template_resampled:  peak-to-peak = 85 us at 100/200/400 ns  - PASS")


if __name__ == "__main__":
    print("MatchedFilter tests (no hardware required)")
    print("=" * 50)
    test_detects_clean_bipolar()
    test_detects_subthreshold_bipolar()
    test_rejects_noise_spike()
    test_duration_validation_rejects_wrong_duration()
    test_both_polarities()
    test_amplitude_extraction_accuracy()
    test_scale_1_0_matches_baseline()
    test_scale_2_0_detects_stretched_pulse()
    test_deduplication_keeps_best_score()
    test_matched_scale_recorded()
    test_empty_scale_list_raises()
    test_empirical_template_resampled_to_acquisition_interval()
    print()
    print("All MatchedFilter tests passed.")
