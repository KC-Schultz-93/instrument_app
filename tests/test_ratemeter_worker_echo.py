"""
test_ratemeter_worker_echo.py
-------------------------------
Unit tests for RatemeterWorker's echo-validation wiring (see
docs/runt_trigger_plan.md): per-segment accept/reject gating of band
counting, the validated-vs-all-trigger rate counters, and the
echo-is-prior-trigger duplicate rejection. No hardware required —
_process_record() is exercised directly with hand-built WaveformRecords, so
the worker's QThread is never started.

Run with:
    python tests/test_ratemeter_worker_echo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from instrument_app.services.daq_models import (
    AmplitudeBand,
    EchoValidationConfig,
    MatchedFilterConfig,
    RatemeterConfig,
    WaveformRecord,
)
from instrument_app.services.ratemeter_worker import RatemeterWorker

from test_matched_filter import make_bipolar_trace, _SI_NS, _N

_NARROW_PULSE_US = 30.0
_MF_CONFIG = MatchedFilterConfig(
    enabled=True, pulse_duration_us=_NARROW_PULSE_US, pulse_min_us=20.0, pulse_max_us=45.0,
)
_ECHO_CONFIG = EchoValidationConfig(
    enabled=True, echo_min_us=80.0, echo_max_us=150.0, echo_corr_threshold=0.8,
)


def _make_worker(echo_config=_ECHO_CONFIG):
    config = RatemeterConfig(
        channel="A", voltage_range_v=0.01, coupling="DC",
        sample_interval_ns=_SI_NS, window_duration_ms=_N * _SI_NS / 1e6,
        rate_averaging_s=5.0,
        bands=[AmplitudeBand(label="Band 1", low_mv=-100.0, high_mv=100.0, color="#4fc3f7")],
    )
    return RatemeterWorker(
        service=object(),  # unused by _process_record / _apply_echo_validation
        config=config,
        mf_config=_MF_CONFIG,
        trigger_enabled=False,
        trigger_threshold_v=0.0,
        trigger_direction="RISING",
        echo_config=echo_config,
    )


def _make_record(event_times_us, trigger_offset_ns=0.0):
    time_ns, voltage = make_bipolar_trace(
        _N, _SI_NS, _NARROW_PULSE_US, amplitude_v=0.05, noise_v=0.0,
        event_times_us=event_times_us,
    )
    return WaveformRecord(
        trace_id=0, run_id="test", timestamp=None, voltage=voltage, time_ns=time_ns,
        sample_interval_ns=_SI_NS, config=None,
        metadata={"trigger_offset_ns": trigger_offset_ns},
    )


def test_rejected_segment_excluded_from_band_counts():
    """A segment with no echo is excluded from the band hit deque, but still
    counted in the all-trigger diagnostic."""
    worker = _make_worker()
    record = _make_record([400.0])  # single pulse, no echo -> rejected

    worker._process_record(record)

    assert len(worker._hit_times["Band 1"]) == 0, "Rejected segment's peak should not count toward the band"
    assert len(worker._all_trigger_times) == 1, "All-trigger counter should still increment"
    assert len(worker._validated_trigger_times) == 0
    assert record.metadata["echo_validated"] is False
    assert record.metadata["echo_reason"] == "no_echo"
    print("  rejected_segment_excluded_from_band_counts:  all=1, validated=0  - PASS")


def test_accepted_segment_feeds_band_counts():
    """A segment with a valid echo feeds both of its peaks into the band
    deque and increments both the all-trigger and validated counters."""
    worker = _make_worker()
    record = _make_record([400.0, 512.0], trigger_offset_ns=0.0)  # 112 us echo, in-window

    worker._process_record(record)

    assert len(worker._hit_times["Band 1"]) == 2, (
        f"Expected both peaks of an accepted segment to count, got {len(worker._hit_times['Band 1'])}"
    )
    assert len(worker._all_trigger_times) == 1
    assert len(worker._validated_trigger_times) == 1
    assert record.metadata["echo_validated"] is True
    print("  accepted_segment_feeds_band_counts:  2 peaks counted, validated=1  - PASS")


def test_echo_is_prior_trigger_rejected():
    """A second segment whose first pulse coincides (in absolute,
    batch-relative device time) with the first segment's validated echo is
    rejected as a duplicate trigger, overriding its own otherwise-valid
    accept decision."""
    worker = _make_worker()

    record_a = _make_record([400.0, 512.0], trigger_offset_ns=0.0)
    worker._process_record(record_a)
    assert record_a.metadata["echo_validated"] is True
    # Segment A's echo sits at absolute time 0 + 512.0 us = 512000 ns.

    # Segment B's trigger starts 112 us (= 512000 - 400000 ns) after A's, so
    # its own "first pulse" at local 400.0 us lands at the same absolute
    # device time as A's echo.
    record_b = _make_record([400.0, 512.0], trigger_offset_ns=112_000.0)
    worker._process_record(record_b)

    assert record_b.metadata["echo_validated"] is False, (
        "Segment B should be rejected as a duplicate of A's echo re-triggering"
    )
    assert record_b.metadata["echo_reason"] == "echo_is_prior_trigger", (
        f"Expected 'echo_is_prior_trigger', got {record_b.metadata['echo_reason']!r}"
    )
    assert len(worker._all_trigger_times) == 2, "Both segments should count as triggers"
    assert len(worker._validated_trigger_times) == 1, "Only segment A should count as validated"
    print("  echo_is_prior_trigger_rejected:  B rejected as duplicate of A's echo  - PASS")


def test_rates_payload_includes_all_and_validated_when_echo_enabled():
    worker = _make_worker()
    worker._loop_start = 0.0
    record = _make_record([400.0, 512.0])
    worker._process_record(record)

    payload = worker._compute_rates(now=1.0)
    assert payload["all_rate_hz"] is not None
    assert payload["validated_rate_hz"] is not None
    assert payload["all_rate_hz"] == payload["validated_rate_hz"] == 1.0, (
        f"Expected 1.0 Hz for both (1 event / 1.0 s elapsed), got "
        f"all={payload['all_rate_hz']}, validated={payload['validated_rate_hz']}"
    )
    print("  rates_payload_includes_all_and_validated_when_echo_enabled:  both=1.0 Hz  - PASS")


def test_rates_payload_omits_counters_when_echo_disabled():
    """With echo validation off, the diagnostic counters are None (not 0 —
    they were never populated) rather than silently misleading the UI."""
    worker = _make_worker(echo_config=EchoValidationConfig(enabled=False))
    worker._loop_start = 0.0
    record = _make_record([400.0, 512.0])
    worker._process_record(record)

    payload = worker._compute_rates(now=1.0)
    assert payload["all_rate_hz"] is None
    assert payload["validated_rate_hz"] is None
    # Echo validation off -> normal (unfiltered) band counting still applies.
    assert len(worker._hit_times["Band 1"]) == 2
    print("  rates_payload_omits_counters_when_echo_disabled:  both=None, band counting unaffected  - PASS")


if __name__ == "__main__":
    print("RatemeterWorker echo-validation tests (no hardware required)")
    print("=" * 50)
    test_rejected_segment_excluded_from_band_counts()
    test_accepted_segment_feeds_band_counts()
    test_echo_is_prior_trigger_rejected()
    test_rates_payload_includes_all_and_validated_when_echo_enabled()
    test_rates_payload_omits_counters_when_echo_disabled()
    print()
    print("All RatemeterWorker echo-validation tests passed.")
