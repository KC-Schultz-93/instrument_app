"""
echo_validator.py
------------------
Post-capture validator for the echo-validated runt trigger (see
docs/runt_trigger_plan.md): a captured segment is kept only if a second
pulse, shape-matching the first, follows within a configurable time window.

Stateless between segments — instantiate once per run (sharing the caller's
MatchedFilter instance) and reuse. No Qt, no hardware access, no file I/O.
"""
from __future__ import annotations

from typing import List

import numpy as np

from instrument_app.services.daq_models import (
    BipolarEventRecord,
    EchoValidationConfig,
    EchoValidationResult,
)
from instrument_app.services.matched_filter import MatchedFilter


class EchoValidator:
    """
    Validates one captured segment against EchoValidationConfig by reusing
    the caller's MatchedFilter.detect() — it does not re-implement
    correlation or pulse detection.
    """

    def __init__(self, config: EchoValidationConfig, matcher: MatchedFilter) -> None:
        self._config = config
        self._matcher = matcher

    def validate(self, voltage: np.ndarray, time_ns: np.ndarray) -> EchoValidationResult:
        """
        Run matched-filter detection, then validate via validate_events().
        Standalone convenience for callers (tests, offline scripts) that
        don't already have a detect() result; a caller that already ran
        detect() for its own purposes (e.g. peak listing) should call
        validate_events() directly instead, to avoid detecting twice.
        """
        events = self._matcher.detect(voltage, time_ns)
        return self.validate_events(events)

    def validate_events(self, events: List[BipolarEventRecord]) -> EchoValidationResult:
        """
        Validate an already-detected event list (sorted by time_us, as
        MatchedFilter.detect() returns it): take the earliest as the "first
        pulse", then look for a second within [echo_min_us, echo_max_us] after it.
        """
        cfg = self._config
        if not events:
            return EchoValidationResult(accepted=False, reason="no_first_pulse")

        first = events[0]  # detect() returns events sorted by time_us
        candidates = [
            e for e in events[1:]
            if cfg.echo_min_us <= (e.time_us - first.time_us) <= cfg.echo_max_us
        ]
        if not candidates:
            return EchoValidationResult(
                accepted=False,
                reason="no_echo",
                first_pulse_time_us=first.time_us,
            )

        echo = max(candidates, key=lambda e: e.correlation_score)
        echo_lag_us = echo.time_us - first.time_us

        if echo.correlation_score < cfg.echo_corr_threshold:
            return EchoValidationResult(
                accepted=False,
                reason="low_correlation",
                first_pulse_time_us=first.time_us,
                echo_time_us=echo.time_us,
                echo_lag_us=echo_lag_us,
                echo_correlation=echo.correlation_score,
            )

        amplitude_ratio = None
        if cfg.amp_ratio_min > 0:
            lo = min(abs(first.amplitude_v), abs(echo.amplitude_v))
            hi = max(abs(first.amplitude_v), abs(echo.amplitude_v))
            amplitude_ratio = lo / hi if hi > 0 else 0.0
            if amplitude_ratio < cfg.amp_ratio_min:
                return EchoValidationResult(
                    accepted=False,
                    reason="amplitude_ratio",
                    first_pulse_time_us=first.time_us,
                    echo_time_us=echo.time_us,
                    echo_lag_us=echo_lag_us,
                    echo_correlation=echo.correlation_score,
                    amplitude_ratio=amplitude_ratio,
                )

        return EchoValidationResult(
            accepted=True,
            reason="ok",
            first_pulse_time_us=first.time_us,
            echo_time_us=echo.time_us,
            echo_lag_us=echo_lag_us,
            echo_correlation=echo.correlation_score,
            amplitude_ratio=amplitude_ratio,
        )
