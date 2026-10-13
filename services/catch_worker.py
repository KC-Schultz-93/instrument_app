"""
catch_worker.py
----------------
Catch-capture pipeline for the Ratemeter page.

    PicoScope --(acquisition thread)--> queue --(analysis thread)--> disk + GUI

  * The ACQUISITION thread only arms the scope, waits for a trigger, reads A+B
    and puts the trace on a small queue, then re-arms immediately. Analysis can
    never delay re-arming the trigger.
  * The ANALYSIS thread pulls traces, classifies them (catch_classifier), saves
    catches as raw CSV (catch_recorder) and discards the rest. File I/O happens
    here, never on the GUI thread.

If analysis ever falls behind and the queue fills, the newest trace is dropped
and counted ("dropped") rather than blocking acquisition, so the counts stay
honest. Traces left in the queue when Stop is pressed are still analysed.

Counts:
  triggers            real triggers (A reached the trigger level)
  catches / non_catches   triggers that were analysed
  dropped             triggers lost because the queue was full
  no_trigger_timeouts blocks that returned only because the scope's auto-trigger
                      timer expired (no real trigger) — NOT counted as triggers
  errors              classifier/save failures (traces are kept, not lost)
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from instrument_app.services.catch_classifier import (
    CatchClassifierConfig,
    CatchResult,
    classify_catch,
    trigger_was_met,
)
from instrument_app.services.catch_recorder import CatchRecorder
from instrument_app.services.daq_models import AcquisitionConfig
from instrument_app.services.picoscope_service import PicoScopeService

_QUEUE_SIZE = 8


class CatchCounters:
    """Thread-safe counters shared by the two threads."""

    _FIELDS = ("triggers", "catches", "non_catches", "dropped",
               "no_trigger_timeouts", "errors")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = {name: 0 for name in self._FIELDS}
        self._started = time.monotonic()

    def add(self, **increments: int) -> None:
        with self._lock:
            for name, inc in increments.items():
                self._values[name] += inc

    def snapshot(self) -> dict:
        with self._lock:
            out = dict(self._values)
        out["elapsed_s"] = time.monotonic() - self._started
        return out

    def reset(self) -> None:
        with self._lock:
            self._values = {name: 0 for name in self._FIELDS}
            self._started = time.monotonic()


@dataclass
class TriggerInfo:
    enabled: bool
    threshold_v: float
    direction: str
    auto_timeout_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "threshold_mv": self.threshold_v * 1000.0,
            "direction": self.direction,
            "auto_timeout_ms": self.auto_timeout_ms,
        }


class CatchAcquisitionWorker(QThread):
    """Arms the scope, reads A+B on each trigger, queues the trace."""

    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        service: PicoScopeService,
        config_a: AcquisitionConfig,
        config_b: AcquisitionConfig,
        trigger: TriggerInfo,
        out_queue: "queue.Queue",
        counters: CatchCounters,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._config_a = config_a
        self._config_b = config_b
        self._trigger = trigger
        self._queue = out_queue
        self._counters = counters
        self._stop_flag = False

    def request_stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        trace_id = 0
        try:
            while not self._stop_flag:
                record = self._service.run_block_dual(
                    self._config_a, self._config_b, abort_check=lambda: self._stop_flag
                )
                if record is None:          # aborted while waiting for a trigger
                    break
                if not trigger_was_met(
                    record.voltage_a, self._trigger.enabled,
                    self._trigger.threshold_v, self._trigger.direction,
                ):
                    self._counters.add(no_trigger_timeouts=1)
                    continue
                trace_id += 1
                record.trace_id = trace_id
                self._counters.add(triggers=1)
                try:
                    self._queue.put_nowait(record)
                except queue.Full:
                    self._counters.add(dropped=1)
        except Exception as exc:            # hardware error ends the run
            self.error_occurred.emit(str(exc))


class CatchAnalysisWorker(QThread):
    """Classifies queued traces, saves catches, discards the rest."""

    trace_ready = pyqtSignal(object, object)    # (DualWaveformRecord, CatchResult)
    counts_changed = pyqtSignal(object)         # dict snapshot
    catch_saved = pyqtSignal(str)               # path of the saved CSV
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        in_queue: "queue.Queue",
        counters: CatchCounters,
        recorder: CatchRecorder,
        classifier_config: CatchClassifierConfig,
        trigger: TriggerInfo,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._queue = in_queue
        self._counters = counters
        self._recorder = recorder
        self._config = classifier_config
        self._trigger = trigger
        self._mirror_text = ""
        self._stop_flag = False

    # Safe to call from the GUI thread while running: each just swaps one
    # reference, which the loop picks up for the next trace.
    def set_classifier_config(self, config: CatchClassifierConfig) -> None:
        self._config = config

    def set_mirror_voltages(self, text: str) -> None:
        self._mirror_text = text

    def request_stop(self) -> None:
        """Finish once the queue is empty."""
        self._stop_flag = True

    def run(self) -> None:
        last_counts: Optional[dict] = None
        while True:
            try:
                record = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop_flag:
                    break
                last_counts = self._emit_counts_if_changed(last_counts)
                continue

            try:
                self._handle(record)
            except Exception as exc:        # never let one trace kill the thread
                self._counters.add(errors=1)
                self.error_occurred.emit(f"Catch analysis error: {exc}")
            last_counts = self._emit_counts_if_changed(last_counts, force=True)

        self._recorder.write_session_summary(
            self._counters.snapshot(), self._config.to_dict(), self._trigger.to_dict()
        )
        self.counts_changed.emit(self._counters.snapshot())

    # ------------------------------------------------------------------

    def _handle(self, record) -> None:
        config = self._config               # one config for the whole trace
        try:
            result = classify_catch(
                record.voltage_a, record.voltage_b, record.sample_interval_ns, config
            )
            kind = "catch" if result.is_catch else None
        except Exception as exc:
            # Do not lose a possibly precious trace because the classifier failed.
            self._counters.add(errors=1)
            result = CatchResult(is_catch=False, reason=f"classifier error: {exc}")
            kind = "unclassified"
            self.error_occurred.emit(f"Classifier error (trace kept): {exc}")

        if kind == "catch":
            self._counters.add(catches=1)
        elif kind is None:
            self._counters.add(non_catches=1)

        if kind is not None:
            try:
                path = self._recorder.save(
                    record, result, self._mirror_text, config.to_dict(),
                    self._trigger.to_dict(), self._counters.snapshot(), kind=kind,
                )
                self.catch_saved.emit(str(path))
            except OSError as exc:
                self._counters.add(errors=1)
                self.error_occurred.emit(f"Could not save waveform: {exc}")

        self.trace_ready.emit(record, result)

    def _emit_counts_if_changed(self, last: Optional[dict], force: bool = False) -> dict:
        snap = self._counters.snapshot()
        compare = {k: v for k, v in snap.items() if k != "elapsed_s"}
        if force or last is None or compare != last:
            self.counts_changed.emit(snap)
        return compare


class CatchPipeline(QObject):
    """Owns both threads. Offers the same stop/wait/terminate surface as a
    single QThread worker so the page can treat it like RatemeterWorker."""

    trace_ready = pyqtSignal(object, object)
    counts_changed = pyqtSignal(object)
    catch_saved = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        service: PicoScopeService,
        config_a: AcquisitionConfig,
        config_b: AcquisitionConfig,
        trigger: TriggerInfo,
        classifier_config: CatchClassifierConfig,
        recorder: CatchRecorder,
        counters: Optional[CatchCounters] = None,
        mirror_voltages: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.counters = counters or CatchCounters()
        self._queue: "queue.Queue" = queue.Queue(maxsize=_QUEUE_SIZE)
        self.acquisition = CatchAcquisitionWorker(
            service, config_a, config_b, trigger, self._queue, self.counters
        )
        self.analysis = CatchAnalysisWorker(
            self._queue, self.counters, recorder, classifier_config, trigger
        )
        self.analysis.set_mirror_voltages(mirror_voltages)

        self.analysis.trace_ready.connect(self.trace_ready)
        self.analysis.counts_changed.connect(self.counts_changed)
        self.analysis.catch_saved.connect(self.catch_saved)
        self.analysis.error_occurred.connect(self.error_occurred)
        self.acquisition.error_occurred.connect(self._on_acquisition_error)

    def start(self) -> None:
        self.analysis.start()
        self.acquisition.start()

    def request_stop(self) -> None:
        self.acquisition.request_stop()
        self.analysis.request_stop()

    def wait(self, timeout_ms: int = 5000) -> bool:
        acq_done = self.acquisition.wait(timeout_ms)
        ana_done = self.analysis.wait(timeout_ms)
        return bool(acq_done and ana_done)

    def terminate(self) -> None:
        for worker in (self.acquisition, self.analysis):
            if worker.isRunning():
                worker.terminate()

    def set_classifier_config(self, config: CatchClassifierConfig) -> None:
        self.analysis.set_classifier_config(config)

    def set_mirror_voltages(self, text: str) -> None:
        self.analysis.set_mirror_voltages(text)

    def _on_acquisition_error(self, message: str) -> None:
        self.analysis.request_stop()        # drain what is queued, then stop
        self.error_occurred.emit(message)
