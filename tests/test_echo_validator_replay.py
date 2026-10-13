"""
test_echo_validator_replay.py
-------------------------------
Offline replay of the real evidence capture (docs/runt_trigger_plan.md) that
motivated this feature: Recorded Data/1900V_reflec_43.csv, a PicoScope CSV
export at 5 MS/s (200 ns/sample), 10004 samples, channels A and B.

CSV format: row 1 header ("Time,Channel A,Channel B"), row 2 units
("(ms),(mV),(mV)"), row 3 blank, data from row 4.

This test is a sanity check, not a detector-tuning exercise: the real pulse
pair in channel A sits at ~665 us / ~778 us (lag ~113 us, within the
default 80-150 us echo window) with normalized correlation ~0.9 — matching
the plan's own measurements. A dedicated MatchedFilterConfig (60 us pulse
duration, correlation_threshold=0.8) is used to isolate that genuine pair
from smaller spurious correlation blips elsewhere in the trace; this is
reasonable threshold tuning for one specific capture, not representative of
general on-instrument settings.

Run with:
    python tests/test_echo_validator_replay.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np

from instrument_app.services.daq_models import EchoValidationConfig, MatchedFilterConfig
from instrument_app.services.echo_validator import EchoValidator
from instrument_app.services.matched_filter import MatchedFilter
from instrument_app.services.waveform_processor import WaveformProcessor

_CSV_PATH = Path(__file__).parent.parent / "Recorded Data" / "1900V_reflec_43.csv"
_SAMPLE_INTERVAL_NS = 200  # 5 MS/s, confirmed against the file's own time column below

_MF_CONFIG = MatchedFilterConfig(
    polarity="both",
    pulse_duration_us=60.0,
    pulse_min_us=30.0,
    pulse_max_us=90.0,
    correlation_threshold=0.8,
    min_distance_us=30.0,
)


def _load_channel_a():
    assert _CSV_PATH.exists(), f"Missing evidence capture: {_CSV_PATH}"
    data = np.genfromtxt(_CSV_PATH, delimiter=",", skip_header=3)
    t_ms, a_mv = data[:, 0], data[:, 1]

    actual_interval_ns = (t_ms[1] - t_ms[0]) * 1e6
    assert abs(actual_interval_ns - _SAMPLE_INTERVAL_NS) < 1.0, (
        f"CSV sample interval {actual_interval_ns:.1f} ns != expected {_SAMPLE_INTERVAL_NS} ns"
    )

    time_ns = np.arange(len(a_mv), dtype=np.float64) * _SAMPLE_INTERVAL_NS
    voltage_v = a_mv / 1000.0
    baseline_mean, _ = WaveformProcessor.estimate_baseline(voltage_v)
    corrected = WaveformProcessor.subtract_baseline(voltage_v, baseline_mean)
    return time_ns, corrected


def test_accepts_real_echo_at_default_window():
    """The real ~113 us echo pair in 1900V_reflec_43.csv is accepted at the
    EchoValidationConfig default window (80-150 us)."""
    time_ns, voltage = _load_channel_a()
    matcher = MatchedFilter(_MF_CONFIG, _SAMPLE_INTERVAL_NS)
    echo_config = EchoValidationConfig(
        enabled=True, echo_min_us=80.0, echo_max_us=150.0, echo_corr_threshold=0.8,
    )
    result = EchoValidator(echo_config, matcher).validate(voltage, time_ns)

    assert result.accepted, f"Expected acceptance, got reason={result.reason!r}"
    assert abs(result.echo_lag_us - 112.0) < 10.0, (
        f"echo_lag_us={result.echo_lag_us} far from the plan's documented ~112 us"
    )
    assert result.echo_correlation >= 0.8
    print(
        f"  accepts_real_echo_at_default_window:  first={result.first_pulse_time_us:.1f} us, "
        f"echo_lag={result.echo_lag_us:.1f} us, corr={result.echo_correlation:.3f}  - PASS"
    )


def test_rejects_real_echo_when_window_excludes_it():
    """The same capture is rejected once the window is narrowed to exclude
    the ~112 us lag (e.g. 20-60 us)."""
    time_ns, voltage = _load_channel_a()
    matcher = MatchedFilter(_MF_CONFIG, _SAMPLE_INTERVAL_NS)
    echo_config = EchoValidationConfig(
        enabled=True, echo_min_us=20.0, echo_max_us=60.0, echo_corr_threshold=0.8,
    )
    result = EchoValidator(echo_config, matcher).validate(voltage, time_ns)

    assert not result.accepted, "Expected rejection once the window excludes the ~112 us echo"
    assert result.reason == "no_echo", f"Expected 'no_echo', got {result.reason!r}"
    print("  rejects_real_echo_when_window_excludes_it:  reason=no_echo  - PASS")


if __name__ == "__main__":
    print("EchoValidator replay tests (1900V_reflec_43.csv, no hardware required)")
    print("=" * 50)
    test_accepts_real_echo_at_default_window()
    test_rejects_real_echo_when_window_excludes_it()
    print()
    print("All EchoValidator replay tests passed.")
