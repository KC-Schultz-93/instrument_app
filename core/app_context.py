"""
Application context: singletons for data pipeline components.

Creates and owns the global SourceManager (device ownership) and
ProcessorWorker (analysis), and wires them to the global bus.
"""
from __future__ import annotations

from PyQt5.QtCore import QThread

from instrument_app.core.bus import bus
from instrument_app.services.sources import SourceManager
from instrument_app.processing.processor_worker import ProcessorWorker


class AppContext:
    def __init__(self) -> None:
        # Source
        self.sources = SourceManager()
        # route source status to bus
        try:
            self.sources.status.connect(bus.status)
        except Exception:
            pass

        # Processor in dedicated thread
        self.proc_thread = QThread()
        self.processor = ProcessorWorker()
        self.processor.moveToThread(self.proc_thread)
        # Wire bus -> processor
        bus.frame_block.connect(self.processor.on_frame_block)
        self.proc_thread.start()


# Module-level singleton
ctx = AppContext()
