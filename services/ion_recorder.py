"""
IonRecorder: subscribes to bus.ions_batch and buffers/export ions to CSV/Parquet.

Uses Polars for efficient appends and exports. Lightweight and optional.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, List

try:
    import polars as pl  # type: ignore
    _HAVE_POLARS = True
except Exception:
    pl = None  # type: ignore
    _HAVE_POLARS = False

from PyQt5.QtCore import QObject, pyqtSlot

from instrument_app.core.bus import bus
from instrument_app.processing.geo_calibration import Ion


class IonRecorder(QObject):
    def __init__(self, out_dir: str | Path):
        super().__init__()
        self.root = Path(out_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        if _HAVE_POLARS:
            self._df = pl.DataFrame({
                "f_hz": pl.Series([], dtype=pl.Float64),
                "amp": pl.Series([], dtype=pl.Float64),
                "E_ev_per_z": pl.Series([], dtype=pl.Float64),
                "V_volts": pl.Series([], dtype=pl.Float64),
                "mz": pl.Series([], dtype=pl.Float64),
                "z": pl.Series([], dtype=pl.Float64),
                "m_amu": pl.Series([], dtype=pl.Float64),
                "quality": pl.Series([], dtype=pl.Utf8),
            })
        else:
            self._rows: List[dict] = []

        bus.ions_batch.connect(self.on_ions)

    @pyqtSlot(object)
    def on_ions(self, ions: object) -> None:
        lst: List[Ion] = list(ions) if isinstance(ions, list) else ions
        if not lst:
            return
        rows = [
            {
                "f_hz": i.f_hz,
                "amp": i.amp,
                "E_ev_per_z": (i.E_ev_per_z if i.E_ev_per_z is not None else float("nan")),
                "V_volts": i.V_volts,
                "mz": (i.mz if i.mz is not None else float("nan")),
                "z": (i.z if i.z is not None else float("nan")),
                "m_amu": (i.m_amu if i.m_amu is not None else float("nan")),
                "quality": getattr(i, 'quality', 'ok'),
            }
            for i in lst
        ]
        if _HAVE_POLARS:
            self._df = pl.concat([self._df, pl.DataFrame(rows)], how="vertical_relaxed")
        else:
            self._rows.extend(rows)

    def export_csv(self, path: str | Path) -> None:
        if _HAVE_POLARS:
            self._df.write_csv(str(path))
        else:
            import csv
            with Path(path).open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["f_hz","amp","E_ev_per_z","V_volts","mz","z","m_amu","quality"])
                w.writeheader()
                w.writerows(self._rows)

