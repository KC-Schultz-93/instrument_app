"""
ProcessorWorker: subscribes to bus.frame_block, runs processing, and emits ions.

Features
- Backpressure policy: ring buffer of size N or 'latest only'.
- Emits status and metrics (dropped frames, frame latency, proc time).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, List, Tuple

import numpy as np
from PyQt5.QtCore import QObject, pyqtSlot

from instrument_app.core.bus import bus
from instrument_app.processing.geo_calibration import (
    configure_processing,
    process_frame,
    process_frame_with_event,
    CDMSCalibration,
    build_charge_cal_from_calibration,
    Ion,
    set_profile_callback,
)


@dataclass
class ProcConfig:
    buffer_mode: str = "latest"   # "latest" | "ring"
    buffer_size: int = 4           # only used when buffer_mode == 'ring'
    min_fs_change_reconfig: float = 1e-6  # fractional change to trigger reconfigure
    target_proc_ms: float = 30.0   # soft target for per-frame processing


class ProcessorWorker(QObject):
    def __init__(self) -> None:
        super().__init__()
        self._cfg = ProcConfig()
        self._queue: Deque[Tuple[np.ndarray, float, float]] = deque()
        self._calib: Optional[CDMSCalibration] = None
        self._charge_cal = None
        self._configured_fs: Optional[float] = None
        self._calib_path: Optional[str] = None
        self._E_default: float = 1_000.0
        self._V_default: Optional[float] = None
        self._processing = False
        self._dropped = 0
        self._profiling_enabled = True
        self._last_profile: Optional[dict] = None
        self._ts_window: Deque[float] = deque(maxlen=120)
        self._ema_proc_ms: Optional[float] = None
        if self._profiling_enabled:
            set_profile_callback(self._on_profile)

    # -------------- public setters --------------
    @pyqtSlot(object)
    def set_calibration(self, calib: object) -> None:
        self._calib = calib  # CDMSCalibration
        try:
            self._charge_cal = build_charge_cal_from_calibration(self._calib)  # type: ignore[arg-type]
        except Exception:
            self._charge_cal = None
        # force reconfigure on next frame
        self._configured_fs = None
        bus.status.emit("Processor: calibration set")

    @pyqtSlot(str)
    def set_calibration_source(self, path: str) -> None:
        self._calib_path = str(path)

    @pyqtSlot(float)
    def set_E_default(self, E: float) -> None:
        self._E_default = float(E)

    @pyqtSlot(float)
    def set_V_default(self, V: float) -> None:
        self._V_default = float(V)

    @pyqtSlot(object)
    def set_policy(self, cfg: object) -> None:
        # cfg is dict-like {buffer_mode, buffer_size}
        try:
            mode = str(cfg.get("buffer_mode", self._cfg.buffer_mode))
            size = int(cfg.get("buffer_size", self._cfg.buffer_size))
            self._cfg.buffer_mode = mode if mode in ("latest", "ring") else "latest"
            self._cfg.buffer_size = max(1, size)
        except Exception:
            pass

    @pyqtSlot(bool)
    def set_profiling_enabled(self, enabled: bool) -> None:
        self._profiling_enabled = bool(enabled)
        set_profile_callback(self._on_profile if self._profiling_enabled else None)

    # -------------- input handler --------------
    @pyqtSlot(object, float)
    def on_frame_block(self, x_i16: object, fs_hz: float) -> None:
        now = time.time()
        try:
            x = np.asarray(x_i16, dtype=np.int16)
        except Exception:
            return
        # Enqueue according to policy
        if self._cfg.buffer_mode == "latest":
            self._queue.clear()
            self._queue.append((x, float(fs_hz), now))
        else:  # ring
            self._queue.append((x, float(fs_hz), now))
            while len(self._queue) > self._cfg.buffer_size:
                self._queue.popleft()
                self._dropped += 1
        # Kick processing if idle
        if not self._processing:
            self._process_next()

    # -------------- internals --------------
    def _process_next(self) -> None:
        if not self._queue:
            self._processing = False
            return
        self._processing = True
        x, fs, t_enqueue = self._queue.popleft()
        t0 = time.time()

        # Configure if needed
        if (self._configured_fs is None) or (abs(fs - self._configured_fs) / max(fs, 1.0) > self._cfg.min_fs_change_reconfig):
            configure_processing(fs_hz=fs, charge_cal=self._charge_cal)
            self._configured_fs = fs

        if self._calib is None:
            bus.status.emit("Processor: no calibration; dropping frame")
            self._processing = False
            if self._queue:
                self._process_next()
            return

        # Process
        ev_summary = None
        try:
            x_f32 = x.astype(np.float32, copy=False)
            ions, ev_summary = process_frame_with_event(
                x_f32, self._E_default, self._V_default, calib=self._calib
            )
        except Exception as e:
            bus.status.emit(f"Processor error: {e}")
            ions = []

        t1 = time.time()
        # Emit results
        if ions:
            bus.ions_batch.emit(ions)

        # Persist snippet for ambiguous/multi events
        if isinstance(ev_summary, dict) and ev_summary.get("cls") in ("ambiguous", "multi"):
            snippet_len = min(len(x), 4096)
            raw_snippet = x[:snippet_len]
            bus.event_snippet.emit({
                "cls": ev_summary.get("cls"),
                "n_peaks": ev_summary.get("n_peaks"),
                "n_usable": ev_summary.get("n_usable"),
                "fs_hz": fs,
                "proc_ms": (t1 - t0) * 1000.0,
                "calib_file": self._calib_path,
                "timestamp": t_enqueue,
                "raw_snippet": raw_snippet,
            })

        # Emit metrics
        # Update fps and averages
        self._ts_window.append(t1)
        fps = 0.0
        if len(self._ts_window) >= 2:
            span = self._ts_window[-1] - self._ts_window[0]
            if span > 0:
                fps = (len(self._ts_window) - 1) / span
        proc_ms = (t1 - t0) * 1000.0
        if self._ema_proc_ms is None:
            self._ema_proc_ms = proc_ms
        else:
            self._ema_proc_ms = 0.9 * self._ema_proc_ms + 0.1 * proc_ms

        metrics = {
            "queued": len(self._queue),
            "dropped": self._dropped,
            "latency_ms": (t1 - t_enqueue) * 1000.0,
            "proc_ms": proc_ms,
            "avg_proc_ms": float(self._ema_proc_ms),
            "fps": fps,
            "dropping": bool(self._dropped > 0),
            "fs_hz": fs,
        }
        # Merge profile if available
        if self._last_profile is not None:
            try:
                metrics.update(self._last_profile)
            except Exception:
                pass
            finally:
                self._last_profile = None
        # Notify if over target
        if metrics["proc_ms"] > self._cfg.target_proc_ms:
            bus.status.emit(f"Processor slow: {metrics['proc_ms']:.1f} ms (> {self._cfg.target_proc_ms:.0f} ms)")
        bus.metrics.emit(metrics)

        # Continue if more work
        self._processing = False
        if self._queue:
            self._process_next()

    # -------------- profile callback --------------
    def _on_profile(self, d: dict) -> None:
        self._last_profile = dict(d)
