"""
test_echo_validator.py
------------------------
Unit tests for EchoValidator (post-capture echo validation, see
docs/runt_trigger_plan.md). No hardware required. Reuses make_bipolar_trace
from test_matched_filter.py rather than duplicating the synthetic-waveform
generator.

Tests that place two pulses close together use a narrower pulse_duration_us
(30 us, full-cycle footprint 60 us) than test_matched_filter.py's default
85 us (full-cycle footprint 170 us) so the two injected pulses don't overlap
and additively distort each other's shape at echo-window-relevant spacings
(80-150 us) — a real reflected-particle echo pulse is a short, separate
transient, not a second full synthetic sine cycle overlapping the first.

Run with:
    python tests/test_echo_validator.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from instrument_app.services.daq_models import EchoValidationConfig, MatchedFilterConfig
from instrument_app.services.echo_validator import EchoValidator
from instrument_app.services.matched_filter import MatchedFilter

from test_matched_filter import make_bipolar_trace, make_spike_trace, _SI_NS, _N, _PULSE_US

_DEFAULT_ECHO_CONFIG = EchoValidationConfig(
    enabled=True, echo_min_us=80.0, echo_max_us=150.0, echo_corr_threshold=0.8,
)

# Narrow pulse/template used wherever two pulses sit close together, so their
# full-cycle footprints (2x pulse_duration_us) don't overlap at an echo-window
# spacing. duration validation bounds are narrowed to match.
_NARROW_PULSE_US = 30.0
_NARROW_MF_CONFIG = MatchedFilterConfig(
    pulse_duration_us=_NARROW_PULSE_US, pulse_min_us=20.0, pulse_max_us=45.0,
)


def _make_validator(echo_config=_DEFAULT_ECHO_CONFIG, mf_config=None):
    mf_config = mf_config or MatchedFilterConfig()
    matcher = MatchedFilter(mf_config, _SI_NS)
    return EchoValidator(echo_config, matcher)


def test_accepts_echo_within_window():
    """First pulse + identical echo 112 us later, inside the default window -> accepted."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _NARROW_PULSE_US, amplitude_v=0.05, noise_v=0.0,
        event_times_us=[400.0, 512.0],  # 112 us apart
    )
    validator = _make_validator(mf_config=_NARROW_MF_CONFIG)
    result = validator.validate(voltage, time_ns)
    assert result.accepted, f"Expected acceptance, got reason={result.reason!r}"
    assert abs(result.echo_lag_us - 112.0) < 5.0, (
        f"echo_lag_us={result.echo_lag_us} far from injected 112 us"
    )
    assert result.echo_correlation is not None and result.echo_correlation >= 0.8
    print(f"  accepts_echo_within_window:  echo_lag_us={result.echo_lag_us:.1f}, "
          f"corr={result.echo_correlation:.3f}  - PASS")


def test_rejects_no_echo():
    """Single pulse, no second pulse at all -> rejected, reason='no_echo'."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PULSE_US, amplitude_v=0.05, noise_v=0.0, event_times_us=[400.0],
    )
    validator = _make_validator()
    result = validator.validate(voltage, time_ns)
    assert not result.accepted
    assert result.reason == "no_echo", f"Expected 'no_echo', got {result.reason!r}"
    print("  rejects_no_echo:  reason=no_echo  - PASS")


def test_rejects_echo_outside_window():
    """A second pulse exists but well outside [echo_min_us, echo_max_us] -> rejected."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _PULSE_US, amplitude_v=0.05, noise_v=0.0,
        event_times_us=[400.0, 700.0],  # 300 us apart, outside the 80-150 us window
    )
    validator = _make_validator()
    result = validator.validate(voltage, time_ns)
    assert not result.accepted
    assert result.reason == "no_echo", f"Expected 'no_echo', got {result.reason!r}"
    print("  rejects_echo_outside_window:  reason=no_echo  - PASS")


def test_amplitude_ratio_gate():
    """An echo at ~60% amplitude is rejected when amp_ratio_min excludes it,
    but accepted when amp_ratio_min is off (0)."""
    first_amp = 0.05
    echo_amp = 0.03  # ratio 0.6
    time_ns, v1 = make_bipolar_trace(
        _N, _SI_NS, _NARROW_PULSE_US, amplitude_v=first_amp, noise_v=0.0,
        event_times_us=[400.0],
    )
    # Overlay the smaller echo pulse separately since make_bipolar_trace uses
    # one amplitude for all event_times_us.
    _, echo_only = make_bipolar_trace(
        _N, _SI_NS, _NARROW_PULSE_US, amplitude_v=echo_amp, noise_v=0.0,
        event_times_us=[500.0],  # 100 us gap, no footprint overlap at this pulse width
    )
    voltage = v1 + echo_only

    strict_cfg = EchoValidationConfig(
        enabled=True, echo_min_us=80.0, echo_max_us=150.0,
        echo_corr_threshold=0.8, amp_ratio_min=0.8,
    )
    loose_cfg = EchoValidationConfig(
        enabled=True, echo_min_us=80.0, echo_max_us=150.0,
        echo_corr_threshold=0.8, amp_ratio_min=0.0,
    )

    strict_result = _make_validator(strict_cfg, mf_config=_NARROW_MF_CONFIG).validate(voltage, time_ns)
    assert not strict_result.accepted
    assert strict_result.reason == "amplitude_ratio", (
        f"Expected 'amplitude_ratio', got {strict_result.reason!r}"
    )
    assert strict_result.amplitude_ratio is not None
    assert abs(strict_result.amplitude_ratio - 0.6) < 0.05, (
        f"amplitude_ratio={strict_result.amplitude_ratio} far from expected ~0.6"
    )

    loose_result = _make_validator(loose_cfg, mf_config=_NARROW_MF_CONFIG).validate(voltage, time_ns)
    assert loose_result.accepted, (
        f"Expected acceptance with amp_ratio_min=0 (off), got reason={loose_result.reason!r}"
    )
    print(
        f"  amplitude_ratio_gate:  ratio={strict_result.amplitude_ratio:.2f} "
        "rejected at min=0.8, accepted at min=0.0  - PASS"
    )


def test_rejects_inverted_echo():
    """An echo of opposite polarity is rejected when the matcher is restricted
    to one polarity: a negative_first pulse only partially (anti-)correlates
    with a positive_first-only template (its rising/falling edges give a weak
    partial match, not the ~1.0 a true echo gives), so it fails the
    echo_corr_threshold gate rather than being mistaken for a valid echo."""
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _NARROW_PULSE_US, amplitude_v=0.05, noise_v=0.0,
        event_times_us=[400.0, 512.0],
        polarities=["positive_first", "negative_first"],
    )
    narrow_positive_only = MatchedFilterConfig(
        pulse_duration_us=_NARROW_PULSE_US, pulse_min_us=20.0, pulse_max_us=45.0,
        polarity="positive_first",
    )
    result = _make_validator(mf_config=narrow_positive_only).validate(voltage, time_ns)
    assert not result.accepted
    assert result.reason in ("no_echo", "low_correlation"), (
        f"Expected rejection on shape-mismatch grounds, got {result.reason!r}"
    )
    if result.echo_correlation is not None:
        assert result.echo_correlation < _DEFAULT_ECHO_CONFIG.echo_corr_threshold, (
            f"Inverted echo's correlation {result.echo_correlation} should fall "
            "short of a true echo's ~1.0 match"
        )
    print(f"  rejects_inverted_echo:  reason={result.reason}  - PASS")


def test_rejects_noise_only_segment():
    """A segment with no bipolar pulse at all -> rejected, reason='no_first_pulse'."""
    time_ns, voltage = make_spike_trace(
        _N, _SI_NS, noise_v=0.005, spike_time_us=400.0, spike_amplitude_v=0.02,
    )
    validator = _make_validator()
    result = validator.validate(voltage, time_ns)
    assert not result.accepted
    assert result.reason == "no_first_pulse", f"Expected 'no_first_pulse', got {result.reason!r}"
    print("  rejects_noise_only_segment:  reason=no_first_pulse  - PASS")


def test_rejects_low_correlation_echo():
    """A distorted, poorly-correlating second pulse inside the window is
    rejected by the echo_corr_threshold gate rather than accepted blindly."""
    time_ns, v1 = make_bipolar_trace(
        _N, _SI_NS, _NARROW_PULSE_US, amplitude_v=0.05, noise_v=0.0, event_times_us=[400.0],
    )
    # A short, non-bipolar-shaped spike 100 us later: close enough in time to
    # fall inside the window, but not a shape match, so correlation is low
    # relative to a clean echo.
    _, spike_only = make_spike_trace(
        _N, _SI_NS, noise_v=0.0, spike_time_us=500.0, spike_amplitude_v=0.05, width_samples=3,
    )
    voltage = v1 + spike_only

    strict_cfg = EchoValidationConfig(
        enabled=True, echo_min_us=80.0, echo_max_us=150.0, echo_corr_threshold=0.95,
    )
    result = _make_validator(strict_cfg, mf_config=_NARROW_MF_CONFIG).validate(voltage, time_ns)
    assert not result.accepted
    assert result.reason in ("low_correlation", "no_echo"), (
        f"Expected rejection on correlation grounds, got {result.reason!r}"
    )
    print(f"  rejects_low_correlation_echo:  reason={result.reason}  - PASS")


if __name__ == "__main__":
    print("EchoValidator tests (no hardware required)")
    print("=" * 50)
    test_accepts_echo_within_window()
    test_rejects_no_echo()
    test_rejects_echo_outside_window()
    test_amplitude_ratio_gate()
    test_rejects_inverted_echo()
    test_rejects_noise_only_segment()
    test_rejects_low_correlation_echo()
    print()
    print("All EchoValidator tests passed.")
