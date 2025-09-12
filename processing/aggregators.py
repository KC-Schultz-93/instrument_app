"""
Polars-backed streaming aggregators for high-throughput analysis.

Primary target is real-time histograms for m/z or mass where we may
want to retain per-ion rows for later queries while updating a live
histogram efficiently.

This module is optional: if Polars is unavailable, it falls back to
NumPy-only implementations that match the existing behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

try:
    import polars as pl  # type: ignore
    _HAVE_POLARS = True
except Exception:  # pragma: no cover - optional dependency
    pl = None  # type: ignore
    _HAVE_POLARS = False

from .geo_calibration import Ion  # type: ignore


@dataclass
class HistogramConfig:
    field_name: str = "mz"     # attribute on Ion: "mz" or "m_amu"
    bins: int = 120            # used if bin_width is None
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    bin_width: Optional[float] = None  # fixed bin width, overrides bins if set


class PolarsHistogram:
    """Incremental histogram aggregation using Polars (with NumPy fallback).

    - Maintains an append-only DataFrame of the chosen field for all ions.
    - Computes histogram counts on demand using cut/bin-index grouping.
    - If Polars unavailable, stores only rolling edges/counts using NumPy.
    """

    def __init__(self, cfg: HistogramConfig):
        self.cfg = cfg
        self.total_ions = 0

        # streaming state
        self._vals_min: Optional[float] = None
        self._vals_max: Optional[float] = None

        if _HAVE_POLARS:
            self._df = pl.DataFrame({"val": pl.Series([], dtype=pl.Float64)})
        else:
            self._edges: Optional[np.ndarray] = None
            self._counts: Optional[np.ndarray] = None

    # --------------- Public API ---------------
    def update(self, ions: List[Ion]) -> None:
        if not ions:
            return
        vals = np.array([
            getattr(i, self.cfg.field_name)
            for i in ions
            if getattr(i, self.cfg.field_name) is not None
        ], dtype=float)
        if vals.size == 0:
            return
        self.total_ions += int(vals.size)
        # track min/max
        vmin, vmax = float(np.min(vals)), float(np.max(vals))
        self._vals_min = vmin if self._vals_min is None else min(self._vals_min, vmin)
        self._vals_max = vmax if self._vals_max is None else max(self._vals_max, vmax)

        if _HAVE_POLARS:
            self._df = pl.concat([self._df, pl.DataFrame({"val": vals})], how="vertical_relaxed")
        else:
            # Basic rolling numpy histogram
            edges, counts = self._ensure_edges(vals)
            hist, _ = np.histogram(vals, bins=edges)
            self._counts += hist

    def histogram(self) -> Tuple[np.ndarray, np.ndarray]:
        """Return (edges, counts) arrays suitable for plotting as a bar chart."""
        if _HAVE_POLARS:
            if self._df.height == 0:
                return np.array([], dtype=float), np.array([], dtype=int)
            vmin, vmax, edges = self._compute_edges()
            if edges.size < 2:
                return edges, np.array([], dtype=int)
            # Compute bin index for each row, then count per bin
            bw = float(edges[1] - edges[0])
            # Avoid division by zero
            if bw <= 0:
                return edges, np.zeros(edges.size - 1, dtype=int)
            df = self._df.with_columns([
                pl.lit(vmin).alias("vmin"),
                ((pl.col("val") - pl.lit(vmin)) / bw).floor().cast(pl.Int64).alias("bin")
            ])
            # Clamp bins to [0, nbins-1]
            nb = edges.size - 1
            df = df.with_columns([
                pl.when(pl.col("bin") < 0).then(0)
                 .when(pl.col("bin") >= nb).then(nb - 1)
                 .otherwise(pl.col("bin")).alias("bin")
            ])
            grouped = df.group_by("bin").len().sort("bin")
            bins = np.arange(nb, dtype=int)
            counts = np.zeros(nb, dtype=int)
            if grouped.height:
                idx = grouped["bin"].to_numpy()
                cnt = grouped["len"].to_numpy()
                counts[idx] = cnt
            return edges, counts
        else:
            edges = self._edges if hasattr(self, "_edges") else None
            counts = self._counts if hasattr(self, "_counts") else None
            if edges is None or counts is None:
                return np.array([], dtype=float), np.array([], dtype=int)
            return edges, counts

    # --------------- Internals ---------------
    def _compute_edges(self) -> Tuple[float, float, np.ndarray]:
        # Decide edges from config/min/max
        vmin = self.cfg.vmin if self.cfg.vmin is not None else (self._vals_min or 0.0)
        vmax = self.cfg.vmax if self.cfg.vmax is not None else (self._vals_max or vmin)
        if vmin == vmax:
            # Expand degenerate range slightly
            vmin *= 0.9
            vmax *= 1.1 if vmax != 0 else 1.0
        if self.cfg.bin_width and self.cfg.bin_width > 0:
            bw = float(self.cfg.bin_width)
            nbins = max(1, int(np.ceil((vmax - vmin) / bw)))
            edges = vmin + np.arange(nbins + 1, dtype=float) * bw
        else:
            nbins = int(self.cfg.bins)
            edges = np.linspace(vmin, vmax, nbins + 1)
        return vmin, vmax, edges

    def _ensure_edges(self, vals: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        # NumPy fallback: initialize or expand histogram edges
        if getattr(self, "_edges", None) is None or getattr(self, "_counts", None) is None:
            _, _, edges = self._compute_edges()
            counts = np.zeros(edges.size - 1, dtype=int)
            self._edges, self._counts = edges, counts
        else:
            # Expand if needed
            vmin, vmax = float(np.min(vals)), float(np.max(vals))
            if vmin < self._edges[0] or vmax > self._edges[-1]:
                _, _, edges = self._compute_edges()
                self._edges = edges
                self._counts = np.zeros(edges.size - 1, dtype=int)
        return self._edges, self._counts

