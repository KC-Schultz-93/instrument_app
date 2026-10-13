"""
ratemeter_worker.py
--------------------
QThread worker for the Ratemeter page.

Runs a continuous block-mode acquisition loop and, per waveform window:
  1. Estimates and subtracts baseline
  2. Finds peaks (reuses SignalExtractor)
  3. Bins each peak into whichever AmplitudeBand its amplitude falls in
  4. Emits rolling rates (Hz) for each band every ~0.5 s wall-clock

No file I/O, no CDMS physics, no logging.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from instrument_app.services.picoscope_service import PicoScopeService
from instrument_app.services.daq_models import (
    EchoValidationConfig,
    EchoValidationResult,
    MatchedFilterConfig,
    PeakRecord,
    RatemeterConfig,
    RatemeterEvent,
)
from instrument_app.services.waveform_processor import WaveformProcessor
from instrument_app.services.signal_extractor import SignalExtractor


class RatemeterWorker(QThread):
    rates_updated = pyqtSignal(object)       # dict with "rates"/"transits"/"fractions"/"velocities"/"all_rate_hz"/"validated_rate_hz"
    waveform_ready = pyqtSignal(object)       # WaveformRecord  (latest trace, for plot)
    error_occurred = pyqtSignal(str)
    status_update = pyqtSignal(str)
    trace_count_changed = pyqtSignal(int)     # total windows acquired
    peak_event = pyqtSignal(object)           # RatemeterEvent — one per detected peak, always emitted
    raw_peaks_detected = pyqtSignal(object)   # List[PeakRecord] — every peak in this record, pre band-match
    correlation_ready = pyqtSignal(object)    # np.ndarray — normalized correlation, matched filter mode only
    segment_validated = pyqtSignal(object, object)  # (WaveformRecord, EchoValidationResult) — echo validation on, one per segment

    _RATE_EMIT_INTERVAL_S = 0.5

    def __init__(
        self,
        service: PicoScopeService,
        config: RatemeterConfig,
        mf_config: MatchedFilterConfig,
        trigger_enabled: bool,
        trigger_threshold_v: float,
        trigger_direction: str,
        captures_per_batch: int = 1,
        trigger_threshold_lower_v: Optional[float] = None,
        trigger_hysteresis_v: float = 0.0,
        echo_config: Optional[EchoValidationConfig] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._config = config
        self._acq_config = config.to_acquisition_config(
            trigger_enabled, trigger_threshold_v, trigger_direction,
            trigger_threshold_lower_v, trigger_hysteresis_v,
        )
        self._captures_per_batch = max(1, captures_per_batch)
        self._extractor = SignalExtractor()

        self._mf_config = mf_config
        if mf_config.enabled:
            from instrument_app.services.matched_filter import MatchedFilter
            self._matcher = MatchedFilter(mf_config, config.sample_interval_ns)
        else:
            self._matcher = None

        # Echo validation reuses the matched-filter detection pipeline (see
        # docs/runt_trigger_plan.md), so it only activates when matched-filter
        # mode is also enabled — with simple-threshold detection there is no
        # shape-correlation score to validate an echo against.
        self._echo_config = echo_config
        if echo_config is not None and echo_config.enabled and self._matcher is not None:
            from instrument_app.services.echo_validator import EchoValidator
            self._echo_validator = EchoValidator(echo_config, self._matcher)
        else:
            self._echo_validator = None

        self._stop_flag = False
        self._trace_id = 0
        self._hit_times: Dict[str, deque] = {b.label: deque() for b in config.bands}
        self._transit_times: Dict[str, deque] = {b.label: deque() for b in config.bands}
        self._recent_velocities: Dict[str, deque] = {b.label: deque() for b in config.bands}
        # velocity deque stores (timestamp_monotonic, velocity_m_s) tuples
        self._loop_start = 0.0
        self._last_rate_emit = 0.0

        # All-trigger vs. validated-event rate counters (independent of band
        # membership) — only populated when echo validation is active.
        self._all_trigger_times: deque = deque()
        self._validated_trigger_times: deque = deque()
        # Absolute (batch-relative) device time of the most recently accepted
        # segment's echo pulse, for detecting a segment whose "first pulse"
        # is actually that same echo re-triggering. Reset at the start of
        # every run_rapid_block() batch (see docstring on _process_record).
        self._last_validated_echo_abs_ns: Optional[float] = None

    def request_stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        self._loop_start = time.monotonic()
        try:
            while not self._stop_flag:
                if self._captures_per_batch <= 1:
                    records = [self._service.run_block(self._acq_config)]
                else:
                    records = self._service.run_rapid_block(
                        self._acq_config, self._captures_per_batch
                    )

                # trigger_offset_ns (when present) is relative to this batch's
                # RunBlock call only — comparisons across batches aren't
                # meaningful, so the echo-duplicate tracker resets here.
                self._last_validated_echo_abs_ns = None

                for record in records:
                    self._trace_id += 1
                    record.trace_id = self._trace_id
                    self._process_record(record)

                now = time.monotonic()
                if now - self._last_rate_emit >= self._RATE_EMIT_INTERVAL_S:
                    self._last_rate_emit = now
                    self.rates_updated.emit(self._compute_rates(now))
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return

        self.status_update.emit("Stopped")

    def _process_record(self, record) -> None:
        baseline_mean, baseline_rms = WaveformProcessor.estimate_baseline(record.voltage)
        corrected = WaveformProcessor.subtract_baseline(record.voltage, baseline_mean)

        if self._matcher is not None:
            # --- Matched filter path ---
            events = self._matcher.detect(corrected, record.time_ns)
            peaks = [
                PeakRecord(
                    peak_index=evt.event_index,
                    time_ns=evt.time_us * 1_000.0,
                    amplitude_v=evt.amplitude_v,
                    width_samples=None,
                    width_ns=None,
                    rise_ns=None,
                    fall_ns=None,
                )
                for evt in events
            ]

            if self._echo_validator is not None:
                peaks = self._apply_echo_validation(record, events, peaks)
        else:
            # --- Simple threshold path ---
            # Use the lowest band boundary (×0.9) as the height threshold so
            # any peak that could fall in a band is detected.  A sigma-based
            # threshold would be set at the signal amplitude for continuous
            # signals (no quiet pre-trigger baseline), silencing all peaks.
            min_band_v = (
                min(b.low_mv for b in self._config.bands) / 1000.0
                if self._config.bands else None
            )
            height_override = min_band_v * 0.9 if min_band_v else None

            peaks = self._extractor.find_peaks(
                corrected, baseline_mean, baseline_rms, record.time_ns,
                height_threshold_v=height_override,
                width_rel_height=self._config.width_rel_height,
            )

        self.raw_peaks_detected.emit(peaks)

        now = time.monotonic()
        now_dt = datetime.now()
        L = self._config.electrode_length_m

        for band in self._config.bands:
            dq = self._hit_times[band.label]
            transit_dq = self._transit_times[band.label]
            vel_dq = self._recent_velocities[band.label]

            for peak in peaks:
                amp_mv = peak.amplitude_v * 1000.0
                if not (band.low_mv <= amp_mv <= band.high_mv):
                    continue

                dq.append(now)
                w_ns = peak.width_ns

                if self._matcher is not None:
                    # Matched-filter events have no peak width, so transit/splat
                    # classification (which depends on width) does not apply.
                    event_type = "unknown"
                    velocity = None
                    transit_us = None
                elif band.transit_min_width_ns is None:
                    event_type = "unknown"
                    velocity = None
                    transit_us = None
                elif w_ns is not None and w_ns >= band.transit_min_width_ns:
                    event_type = "transit"
                    transit_us = w_ns / 1000.0
                    velocity = L / (w_ns * 1e-9) if w_ns > 0 else None
                    transit_dq.append(now)
                    if velocity is not None:
                        vel_dq.append((now, velocity))
                else:
                    event_type = "splat"
                    transit_us = None
                    velocity = None

                self.peak_event.emit(RatemeterEvent(
                    timestamp=now_dt,
                    band_label=band.label,
                    amplitude_mv=amp_mv,
                    width_ns=w_ns,
                    event_type=event_type,
                    velocity_m_s=velocity,
                    transit_time_us=transit_us,
                ))

        if self._matcher is not None:
            self.correlation_ready.emit(self._matcher.last_correlation)
        self.waveform_ready.emit(record)
        self.trace_count_changed.emit(self._trace_id)

    def _apply_echo_validation(self, record, events, peaks: List[PeakRecord]) -> List[PeakRecord]:
        """
        Run echo validation for one segment (once per segment, not per peak):
        on rejection, this segment's peaks are excluded from band counting
        (returns []) so the main rate display reflects validated events only,
        per docs/runt_trigger_plan.md's own recommendation — the "all
        triggers" count (self._all_trigger_times) is still bumped regardless,
        as a secondary diagnostic for judging the false-trigger rate.

        Also rejects a segment whose first pulse coincides with the previous
        segment's validated echo (the echo re-triggering its own segment —
        see run_rapid_block()'s trigger_offset_ns), overriding the
        validator's own per-segment result.
        """
        result = self._echo_validator.validate_events(events)

        trigger_offset_ns = record.metadata.get("trigger_offset_ns")
        if (
            result.accepted
            and trigger_offset_ns is not None
            and self._last_validated_echo_abs_ns is not None
            and result.first_pulse_time_us is not None
        ):
            first_abs_ns = trigger_offset_ns + result.first_pulse_time_us * 1_000.0
            tolerance_ns = 2.0 * record.sample_interval_ns
            if abs(first_abs_ns - self._last_validated_echo_abs_ns) <= tolerance_ns:
                result = EchoValidationResult(
                    accepted=False,
                    reason="echo_is_prior_trigger",
                    first_pulse_time_us=result.first_pulse_time_us,
                    echo_time_us=result.echo_time_us,
                    echo_lag_us=result.echo_lag_us,
                    echo_correlation=result.echo_correlation,
                    amplitude_ratio=result.amplitude_ratio,
                )

        now = time.monotonic()
        self._all_trigger_times.append(now)
        if result.accepted:
            self._validated_trigger_times.append(now)
            if trigger_offset_ns is not None and result.echo_time_us is not None:
                self._last_validated_echo_abs_ns = (
                    trigger_offset_ns + result.echo_time_us * 1_000.0
                )

        record.metadata["echo_validated"] = result.accepted
        record.metadata["echo_reason"] = result.reason
        record.metadata["echo_first_pulse_us"] = result.first_pulse_time_us
        record.metadata["echo_lag_us"] = result.echo_lag_us
        record.metadata["echo_correlation"] = result.echo_correlation
        record.metadata["echo_amp_ratio"] = result.amplitude_ratio

        self.segment_validated.emit(record, result)

        return peaks if result.accepted else []

    def _compute_rates(self, now: float) -> dict:
        cutoff = now - self._config.rate_averaging_s
        elapsed = min(now - self._loop_start, self._config.rate_averaging_s)

        rates: Dict[str, float] = {}
        transits: Dict[str, float] = {}
        fractions: Dict[str, Optional[float]] = {}
        velocities: Dict[str, Optional[float]] = {}

        for band in self._config.bands:
            dq = self._hit_times[band.label]
            while dq and dq[0] < cutoff:
                dq.popleft()

            tdq = self._transit_times[band.label]
            while tdq and tdq[0] < cutoff:
                tdq.popleft()

            vdq = self._recent_velocities[band.label]
            while vdq and vdq[0][0] < cutoff:
                vdq.popleft()

            n_all = len(dq)
            n_transit = len(tdq)
            rate = n_all / elapsed if elapsed > 0 else 0.0
            t_rate = n_transit / elapsed if elapsed > 0 else 0.0

            rates[band.label] = rate
            transits[band.label] = t_rate
            fractions[band.label] = (
                (n_transit / n_all * 100.0)
                if (n_all > 0 and band.transit_min_width_ns is not None)
                else None
            )
            velocities[band.label] = (
                sum(v for _, v in vdq) / len(vdq) if vdq else None
            )

        all_rate_hz: Optional[float] = None
        validated_rate_hz: Optional[float] = None
        if self._echo_validator is not None:
            adq = self._all_trigger_times
            while adq and adq[0] < cutoff:
                adq.popleft()
            vdq2 = self._validated_trigger_times
            while vdq2 and vdq2[0] < cutoff:
                vdq2.popleft()
            all_rate_hz = len(adq) / elapsed if elapsed > 0 else 0.0
            validated_rate_hz = len(vdq2) / elapsed if elapsed > 0 else 0.0

        return {
            "rates": rates,
            "transits": transits,
            "fractions": fractions,
            "velocities": velocities,
            # None when echo validation is off (no secondary diagnostic to show).
            "all_rate_hz": all_rate_hz,
            "validated_rate_hz": validated_rate_hz,
        }
