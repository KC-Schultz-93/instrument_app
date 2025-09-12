"""
Unified acquisition sources that publish to the global bus.

This wraps Synthetic and Pico sources behind a small, common interface
and emits frames via instrument_app.core.bus.bus.frame_block.

Pages can subscribe to the bus instead of wiring scope->worker directly,
and multiple listeners can co-exist without changing the source.
"""
from __future__ import annotations

import time
from typing import Optional

import numpy as np
from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from instrument_app.core.bus import bus


class SyntheticSource(QObject):
    """Synthetic generator that emits int16 frames to the bus."""

    status = pyqtSignal(str)

    def __init__(self, *, fs_hz=2_400_000.0, n_samples=262_144, period_ms=250):
        super().__init__()
        self.fs = float(fs_hz)
        self.N = int(n_samples)
        self.period_ms = int(period_ms)
        self._running = False

    @pyqtSlot()
    def start(self):
        self._running = True
        self.status.emit(f"Synthetic: fs={self.fs:.0f}Hz N={self.N} period={self.period_ms}ms")
        t = np.arange(self.N, dtype=np.float32) / self.fs
        while self._running:
            # simple single-tone + noise demo
            f0 = 60_000.0
            noise_rms = 0.05
            amp = noise_rms * 10
            x = amp * np.sin(2*np.pi*f0*t, dtype=np.float32) + np.random.normal(0.0, noise_rms, size=self.N)
            x16 = np.clip(x * 1000.0, -32767, 32767).astype(np.int16)
            bus.frame_block.emit(x16, self.fs)
            QThread.msleep(self.period_ms)

    @pyqtSlot()
    def stop(self):
        self._running = False


class PicoSource(QObject):
    """Thin wrapper around services.scope_pico to publish to bus."""

    status = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        try:
            from .scope_pico import PicoScopeService  # local import to keep optional
        except Exception as e:  # pragma: no cover - optional dependency
            raise RuntimeError("Pico SDK/wrapper not available") from e
        self._svc = PicoScopeService()
        self._svc.block_ready.connect(self._on_block)
        self._svc.status.connect(self.status)
        # Route scope status/metrics to bus
        try:
            self._svc.scope_status.connect(lambda d: bus.metrics.emit(d))
        except Exception:
            pass

    @pyqtSlot()
    def start_rapid(self):
        self._svc.start_rapid_block()

    @pyqtSlot()
    def start_streaming(self):
        self._svc.start_streaming()

    @pyqtSlot()
    def stop(self):
        self._svc.stop()

    @pyqtSlot(object, float)
    def _on_block(self, x_i16: object, fs_hz: float):
        bus.frame_block.emit(x_i16, float(fs_hz))


class SourceManager(QObject):
    """Owns a single active source and its thread, publishing to the bus.

    This helps ensure only one scope is active at a time and centralizes
    start/stop lifecycle.
    """

    status = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._thr: Optional[QThread] = None
        self._src: Optional[QObject] = None

    def start_synthetic(self, fs_hz: float, n_samples: int, period_ms: int) -> None:
        self.stop()
        self._thr = QThread(); self._src = SyntheticSource(fs_hz=fs_hz, n_samples=n_samples, period_ms=period_ms)
        self._src.moveToThread(self._thr)  # type: ignore[attr-defined]
        # connect status if present
        try:
            self._src.status.connect(self.status)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._thr.started.connect(self._src.start)  # type: ignore[attr-defined]
        self._thr.start()
        self.status.emit("Source: Synthetic started")

    def start_pico_rapid(self) -> None:
        self.stop()
        self._thr = QThread(); self._src = PicoSource()
        self._src.moveToThread(self._thr)  # type: ignore[attr-defined]
        try:
            self._src.status.connect(self.status)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._thr.started.connect(self._src.start_rapid)  # type: ignore[attr-defined]
        self._thr.start()
        self.status.emit("Source: Pico Rapid started")

    def start_pico_streaming(self) -> None:
        self.stop()
        self._thr = QThread(); self._src = PicoSource()
        self._src.moveToThread(self._thr)  # type: ignore[attr-defined]
        try:
            self._src.status.connect(self.status)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._thr.started.connect(self._src.start_streaming)  # type: ignore[attr-defined]
        self._thr.start()
        self.status.emit("Source: Pico Streaming started")

    def stop(self) -> None:
        if self._thr and self._thr.isRunning():
            try:
                # try to call stop slot if present
                self._src.stop()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._thr.quit(); self._thr.wait()
        self._thr = None; self._src = None
        self.status.emit("Source: stopped")
