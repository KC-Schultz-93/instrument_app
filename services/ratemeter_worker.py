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
from typing import Dict

from PyQt5.QtCore import QThread, pyqtSignal

from instrument_app.services.picoscope_service import PicoScopeService
from instrument_app.services.daq_models import RatemeterConfig
from instrument_app.services.waveform_processor import WaveformProcessor
from instrument_app.services.signal_extractor import SignalExtractor


class RatemeterWorker(QThread):
    rates_updated = pyqtSignal(object)       # dict[str, float]  band_label -> Hz
    waveform_ready = pyqtSignal(object)       # WaveformRecord  (latest trace, for plot)
    error_occurred = pyqtSignal(str)
    status_update = pyqtSignal(str)
    trace_count_changed = pyqtSignal(int)     # total windows acquired

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
                peaks = self._extractor.find_peaks(
                    corrected, baseline_mean, baseline_rms, record.time_ns
                )

                now = time.monotonic()
                for band in self._config.bands:
                    dq = self._hit_times[band.label]
                    for peak in peaks:
                        amp_mv = peak.amplitude_v * 1000
                        if band.low_mv <= amp_mv <= band.high_mv:
                            dq.append(now)

                self.waveform_ready.emit(record)
                self.trace_count_changed.emit(self._trace_id)

                if now - self._last_rate_emit >= self._RATE_EMIT_INTERVAL_S:
                    self._last_rate_emit = now
                    self.rates_updated.emit(self._compute_rates(now))
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return

        self.status_update.emit("Stopped")

    def _compute_rates(self, now: float) -> Dict[str, float]:
        cutoff = now - self._config.rate_averaging_s
        elapsed = min(now - self._loop_start, self._config.rate_averaging_s)
        rates: Dict[str, float] = {}
        for band in self._config.bands:
            dq = self._hit_times[band.label]
            while dq and dq[0] < cutoff:
                dq.popleft()
            rates[band.label] = len(dq) / elapsed if elapsed > 0 else 0.0
        return rates
