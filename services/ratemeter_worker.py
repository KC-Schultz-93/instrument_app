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
from typing import Dict, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from instrument_app.services.picoscope_service import PicoScopeService
from instrument_app.services.daq_models import RatemeterConfig, RatemeterEvent
from instrument_app.services.waveform_processor import WaveformProcessor
from instrument_app.services.signal_extractor import SignalExtractor


class RatemeterWorker(QThread):
    rates_updated = pyqtSignal(object)       # dict with "rates"/"transits"/"fractions"/"velocities"
    waveform_ready = pyqtSignal(object)       # WaveformRecord  (latest trace, for plot)
    error_occurred = pyqtSignal(str)
    status_update = pyqtSignal(str)
    trace_count_changed = pyqtSignal(int)     # total windows acquired
    peak_event = pyqtSignal(object)           # RatemeterEvent — one per detected peak, always emitted

    _RATE_EMIT_INTERVAL_S = 0.5

    def __init__(
        self,
        service: PicoScopeService,
        config: RatemeterConfig,
        trigger_enabled: bool,
        trigger_threshold_v: float,
        trigger_direction: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._config = config
        self._acq_config = config.to_acquisition_config(
            trigger_enabled, trigger_threshold_v, trigger_direction
        )
        self._extractor = SignalExtractor()
        self._stop_flag = False
        self._trace_id = 0
        self._hit_times: Dict[str, deque] = {b.label: deque() for b in config.bands}
        self._transit_times: Dict[str, deque] = {b.label: deque() for b in config.bands}
        self._recent_velocities: Dict[str, deque] = {b.label: deque() for b in config.bands}
        # velocity deque stores (timestamp_monotonic, velocity_m_s) tuples
        self._loop_start = 0.0
        self._last_rate_emit = 0.0

    def request_stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        self._loop_start = time.monotonic()
        try:
            while not self._stop_flag:
                record = self._service.run_block(self._acq_config)
                self._trace_id += 1

                baseline_mean, baseline_rms = WaveformProcessor.estimate_baseline(record.voltage)
                corrected = WaveformProcessor.subtract_baseline(record.voltage, baseline_mean)

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

                        if band.transit_min_width_ns is None:
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

                self.waveform_ready.emit(record)
                self.trace_count_changed.emit(self._trace_id)

                if now - self._last_rate_emit >= self._RATE_EMIT_INTERVAL_S:
                    self._last_rate_emit = now
                    self.rates_updated.emit(self._compute_rates(now))
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return

        self.status_update.emit("Stopped")

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

        return {
            "rates": rates,
            "transits": transits,
            "fractions": fractions,
            "velocities": velocities,
        }
