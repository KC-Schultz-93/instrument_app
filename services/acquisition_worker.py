"""
AcquisitionWorker.py
--------------------
QThread worker that runs the continuous block-mode acquisition loop.

The worker owns the acquisition loop and is the only caller of
PicoScopeService.run_block(). All results are returned to the main thread
via Qt signals (QueuedConnection is the automatic default for cross-thread
signal delivery in PyQt5).

Lifecycle:
    worker = AcquisitionWorker(service, config, cdms_config, logger, run_id)
    worker.waveform_ready.connect(page._on_waveform_received)
    worker.cdms_ready.connect(page._on_cdms_received)
    worker.error_occurred.connect(page._on_daq_error)
    worker.start()                  # launches run()
    ...
    worker.request_stop()           # call from main thread
    worker.wait(timeout_ms=5000)    # join; then safe to call logger.close()

Analysis pipeline (per trace):
    run_block â†’ save_raw(+STFT) â†’ estimate_baseline â†’ subtract_baseline â†’
    find_peaks â†’ classify â†’ FFT â†’ STFT â†’ CDMSAnalyzer â†’
    build EventSummary â†’ save_event_summary â†’ emit signals

After loop exits:
    save_mass_histogram â†’ emit status_update
"""

from __future__ import annotations

from typing import List, Tuple

from PyQt5.QtCore import QThread, pyqtSignal

from instrument_app.services.cdms_analyzer import CDMSAnalyzer
from instrument_app.services.daq_logger import DAQLogger
from instrument_app.services.daq_models import (
    AcquisitionConfig,
    CDMSConfig,
    CDMSResult,
    EventSummary,
    STFTResult,
    WaveformRecord,
)
from instrument_app.services.event_detector import EventDetector
from instrument_app.services.picoscope_service import PicoScopeService
from instrument_app.services.signal_extractor import SignalExtractor
from instrument_app.services.waveform_processor import WaveformProcessor


class AcquisitionWorker(QThread):
    """
    Runs the continuous block-mode acquisition loop on a worker thread.

    Parameters
    ----------
    service : PicoScopeService
        Already-connected PicoScope service.
    config : AcquisitionConfig
        Acquisition parameters for this run.
    cdms_config : CDMSConfig
        CDMS physics calibration parameters for this run.
    logger : DAQLogger
        Open DAQ logger for file output.
    run_id : str
        Run identifier string (stamped onto every WaveformRecord and EventSummary).
    """

    # Signals â€” delivered to the main thread's event loop automatically
    waveform_ready = pyqtSignal(object)     # WaveformRecord
    event_ready = pyqtSignal(object)        # EventSummary
    cdms_ready = pyqtSignal(object)         # CDMSResult
    stft_ready = pyqtSignal(object)         # STFTResult
    error_occurred = pyqtSignal(str)
    status_update = pyqtSignal(str)
    trace_count_changed = pyqtSignal(int)   # total traces acquired so far

    def __init__(
        self,
        service: PicoScopeService,
        config: AcquisitionConfig,
        cdms_config: CDMSConfig,
        logger: DAQLogger,
        run_id: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._config = config
        self._logger = logger
        self._run_id = run_id
        self._stop_flag: bool = False
        self._trace_id: int = 0

        # Analysis objects â€” instantiated once and reused across traces
        self._extractor = SignalExtractor()
        self._detector = EventDetector()
        self._cdms = CDMSAnalyzer(cdms_config)
        self._stft_nperseg: int = cdms_config.stft_nperseg

    # ------------------------------------------------------------------
    # Control (call from main thread)
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """
        Signal the worker to stop after the current trace completes.
        Thread-safe for a single bool assignment in CPython.
        """
        self._stop_flag = True

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Acquisition loop â€” runs entirely on the worker thread."""
        self.status_update.emit("Acquisition started.")
        self._logger.log("Acquisition loop started.")

        # Accumulate mass estimates for accepted traces (for end-of-run histogram)
        accepted_masses: List[float] = []

        while not self._stop_flag:
            try:
                record = self._service.run_block(self._config)
                self._trace_id += 1
                record.trace_id = self._trace_id
                record.run_id = self._run_id

                # Full analysis pipeline
                summary, stft_result, cdms_result = self._analyse(record)

                # File I/O
                self._logger.save_raw(record, stft=stft_result)
                self._logger.save_event_summary(summary)

                # Accumulate mass for histogram
                if summary.accepted and summary.mass_Da is not None:
                    accepted_masses.append(summary.mass_Da)

                # Emit results to main thread
                self.waveform_ready.emit(record)
                self.event_ready.emit(summary)
                self.cdms_ready.emit(cdms_result)
                self.stft_ready.emit(stft_result)
                self.trace_count_changed.emit(self._trace_id)

            except Exception as exc:
                self._logger.log(f"Acquisition error: {exc}", level="error")
                self.error_occurred.emit(str(exc))
                break

        # Save mass histogram before signalling completion
        try:
            self._logger.save_mass_histogram(accepted_masses)
            self._logger.log(
                f"Mass histogram saved. Accepted masses: {len(accepted_masses)}"
            )
        except Exception as exc:
            self._logger.log(f"Histogram save failed: {exc}", level="warning")

        self._logger.log(
            f"Acquisition loop stopped. Total traces: {self._trace_id}"
        )
        self.status_update.emit(
            f"Acquisition stopped. Traces acquired: {self._trace_id}"
        )

    # ------------------------------------------------------------------
    # Analysis pipeline (runs on worker thread)
    # ------------------------------------------------------------------

    def _analyse(
        self, record: WaveformRecord
    ) -> Tuple[EventSummary, STFTResult, CDMSResult]:
        """
        Run the full per-trace analysis pipeline.

        Returns (EventSummary, STFTResult, CDMSResult).
        """
        voltage = record.voltage

        # Baseline estimation and correction
        try:
            baseline_mean, baseline_rms = WaveformProcessor.estimate_baseline(voltage)
        except ValueError:
            baseline_mean, baseline_rms = 0.0, 0.0
        corrected = WaveformProcessor.subtract_baseline(voltage, baseline_mean)

        # Peak finding (operate on baseline-corrected signal)
        peaks = self._extractor.find_peaks(
            corrected, baseline_mean, baseline_rms, record.time_ns
        )

        # Classification
        classification, accepted = self._detector.classify(
            record, peaks, baseline_mean, baseline_rms
        )

        # Summary statistics
        stats = self._extractor.summarize(peaks, baseline_mean, baseline_rms)

        # FFT â€” dominant oscillation frequency
        _, _, dominant_freq_hz, _ = WaveformProcessor.compute_fft(
            corrected, record.sample_interval_ns
        )

        # STFT â€” time-frequency evolution
        stft_result = WaveformProcessor.compute_stft(
            corrected, record.sample_interval_ns, self._stft_nperseg
        )

        # CDMS physics
        cdms_result = self._cdms.analyse(
            dominant_freq_hz=dominant_freq_hz,
            mean_peak_height_v=stats.get("mean_peak_height_v"),
        )

        summary = EventSummary(
            trace_id=record.trace_id,
            run_id=record.run_id,
            timestamp=record.timestamp,
            accepted=accepted,
            classification=classification,
            baseline_mean_v=baseline_mean,
            baseline_rms_v=baseline_rms,
            signal_max_v=float(voltage.max()),
            signal_min_v=float(voltage.min()),
            num_peaks=stats["num_peaks"],
            mean_peak_height_v=stats.get("mean_peak_height_v"),
            mean_peak_spacing_ns=stats.get("mean_peak_spacing_ns"),
            oscillation_freq_hz=cdms_result.oscillation_freq_hz,
            charge_e=cdms_result.charge_e,
            mz_Da_per_e=cdms_result.mz_Da_per_e,
            mass_Da=cdms_result.mass_Da,
        )

        return summary, stft_result, cdms_result
