"""
Signal bus for decoupled, cross-module communication.

This provides a single QObject with typed Qt signals that producers
(e.g., scope sources) and consumers (e.g., processing workers, pages)
can import and connect to without tight coupling.

Usage:
    from instrument_app.core.bus import bus
    bus.frame_block.emit(x_i16, fs_hz)     # producer
    bus.ions_batch.emit(list_of_ions)      # processing result
    bus.status.emit("... human readable ...")

Rationale:
- Avoid duplicating per-page wiring of source -> processing -> UI.
- Allow multiple subscribers to the same stream (e.g., both CDMS & Processing pages).
- Make it easy to slot in new processing pipelines without changing pages.
"""
from __future__ import annotations

from PyQt5.QtCore import QObject, pyqtSignal


class SignalBus(QObject):
    """Global event bus using Qt signals.

    Signals
    -------
    frame_block(object, float):
        Emits a raw frame block as a numpy int16 array and a sample rate [Hz].
    ions_batch(object):
        Emits a List[Ion] (see processing.geo_calibration.Ion).
    status(str):
        Emits human-readable status messages from services/pipelines.
    """

    frame_block = pyqtSignal(object, float)
    ions_batch = pyqtSignal(object)
    status = pyqtSignal(str)
    metrics = pyqtSignal(object)  # dict with lightweight metrics


# Module-level singleton
bus = SignalBus()
