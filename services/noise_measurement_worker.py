"""
noise_measurement_worker.py
----------------------------
One-shot QThread worker for the "Measure noise" button (see
docs/runt_trigger_plan.md): captures a single long block with triggering
disabled, using the ratemeter's current channel settings, and reports noise
statistics. Advisory only — never writes back into trigger threshold
controls itself.
"""
from __future__ import annotations

from datetime import datetime

from PyQt5.QtCore import QThread, pyqtSignal

from instrument_app.services.daq_models import AcquisitionConfig, NoiseStatsResult
from instrument_app.services.noise_stats import (
    crossing_rate_hz,
    peak_to_peak,
    rms,
    robust_sigma_mad,
    suggested_threshold,
)
from instrument_app.services.picoscope_service import PicoScopeService


class NoiseMeasurementWorker(QThread):
    result_ready = pyqtSignal(object)   # NoiseStatsResult
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        service: PicoScopeService,
        config: AcquisitionConfig,
        k: float = 5.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._config = config
        self._k = k

    def run(self) -> None:
        try:
            self._service.configure_channel(self._config)
            self._service.set_trigger(self._config)  # trigger_enabled=False -> disables it
            record = self._service.run_block(self._config)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return

        sigma_robust = robust_sigma_mad(record.voltage)
        result = NoiseStatsResult(
            rms_v=rms(record.voltage),
            peak_to_peak_v=peak_to_peak(record.voltage),
            sigma_robust_v=sigma_robust,
            suggested_threshold_v=suggested_threshold(sigma_robust, self._k),
            crossing_rate_hz=crossing_rate_hz(
                record.voltage,
                suggested_threshold(sigma_robust, self._k),
                record.sample_interval_ns,
            ),
            range_v=self._config.voltage_range_v,
            measured_at=datetime.now(),
        )
        self.result_ready.emit(result)
