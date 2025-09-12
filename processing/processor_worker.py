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
    CDMSCalibration,
    build_charge_cal_from_calibration,
    Ion,
)


@dataclass
class ProcConfig:
    buffer_mode: str = "latest"   # "latest" | "ring"
    buffer_size: int = 4           # only used when buffer_mode == 'ring'
    min_fs_change_reconfig: float = 1e-6  # fractional change to trigger reconfigure


class ProcessorWorker(QObject):
    def __init__(self) -> None:
        super().__init__()
        self._cfg = ProcConfig()
        self._queue: Deque[Tuple[np.ndarray, float, float]] = deque()
        self._calib: Optional[CDMSCalibration] = None
        self._charge_cal = None
        self._configured_fs: Optional[float] = None
        self._E_default: float = 1_000.0
        self._V_default: Optional[float] = None
        self._processing = False
        self._dropped = 0

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
        try:
            x_f32 = x.astype(np.float32, copy=False)
            ions: List[Ion] = process_frame(x_f32, self._E_default, self._V_default, calib=self._calib)
        except Exception as e:
            bus.status.emit(f"Processor error: {e}")
            ions = []

        t1 = time.time()
        # Emit results
        if ions:
            bus.ions_batch.emit(ions)

        # Emit metrics
        metrics = {
            "queued": len(self._queue),
            "dropped": self._dropped,
            "latency_ms": (t1 - t_enqueue) * 1000.0,
            "proc_ms": (t1 - t0) * 1000.0,
            "fs_hz": fs,
        }
        bus.metrics.emit(metrics)

        # Continue if more work
        self._processing = False
        if self._queue:
            self._process_next()

