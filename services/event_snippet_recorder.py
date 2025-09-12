"""
EventSnippetRecorder: persists minimal metadata and short raw snippets for
ambiguous or multi-ion events. Useful for post-run triage.

Writes one directory per event under the chosen root, containing:
- meta.json (classification, fs, timings, calibration file name)
- snippet.npy (int16 array of first N samples)
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import numpy as np
from PyQt5.QtCore import QObject, pyqtSlot

from instrument_app.core.bus import bus


class EventSnippetRecorder(QObject):
    def __init__(self, out_dir: str | Path):
        super().__init__()
        self.root = Path(out_dir) / "EventSnippets"
        self.root.mkdir(parents=True, exist_ok=True)
        bus.event_snippet.connect(self.on_event)

    @pyqtSlot(object)
    def on_event(self, meta: object) -> None:
        try:
            d = dict(meta)  # shallow copy
            ts = d.get("timestamp") or 0.0
            when = datetime.fromtimestamp(float(ts))
            stamp = when.strftime("%Y%m%d_%H%M%S_%f")
            ev_dir = self.root / stamp
            ev_dir.mkdir(parents=True, exist_ok=True)
            # pop raw snippet
            raw = d.pop("raw_snippet", None)
            # write meta
            with (ev_dir / "meta.json").open("w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
            # write snippet
            if raw is not None:
                arr = np.asarray(raw, dtype=np.int16)
                np.save(ev_dir / "snippet.npy", arr)
        except Exception:
            pass

