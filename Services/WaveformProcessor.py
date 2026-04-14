"""
WaveformProcessor.py
--------------------
Low-level signal conditioning for acquired waveform traces.

All methods are static — no state is held between traces.
No Qt, no file I/O, no hardware access.

Intended usage (called from AcquisitionWorker per trace):
    mean, rms = WaveformProcessor.estimate_baseline(voltage)
    corrected  = WaveformProcessor.subtract_baseline(voltage, mean)
    if WaveformProcessor.is_clipped(voltage, config.voltage_range_v):
        ...
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
from scipy.ndimage import uniform_filter1d


class WaveformProcessor:
    """Stateless signal conditioning. All methods are static."""

    # ------------------------------------------------------------------
    # Baseline
    # ------------------------------------------------------------------

    @staticmethod
    def estimate_baseline(
        voltage: np.ndarray,
        fraction: float = 0.1,
    ) -> Tuple[float, float]:
        """
        Estimate the baseline mean and RMS noise from the first ``fraction``
        of the trace (assumed to be pre-signal or pre-trigger region).

        Parameters
        ----------
        voltage : ndarray
            Voltage array in volts.
        fraction : float
            Fraction of the trace to use as baseline region (default 0.1 = 10%).

        Returns
        -------
        (mean_v, rms_v) : (float, float)

        Raises
        ------
        ValueError
            If the trace is too short to estimate a baseline.
        """
        n = len(voltage)
        n_baseline = max(1, int(n * fraction))
        if n_baseline < 2:
            raise ValueError(
                f"Trace too short ({n} samples) to estimate baseline "
                f"with fraction={fraction}."
            )
        region = voltage[:n_baseline]
        mean = float(region.mean())
        rms = float(np.sqrt(np.mean((region - mean) ** 2)))
        return mean, rms

    @staticmethod
    def subtract_baseline(voltage: np.ndarray, baseline_mean: float) -> np.ndarray:
        """Return a new array with the DC offset removed."""
        return voltage - baseline_mean

    # ------------------------------------------------------------------
    # Polarity
    # ------------------------------------------------------------------

    @staticmethod
    def invert(voltage: np.ndarray) -> np.ndarray:
        """Negate the voltage array (for negative-going signals)."""
        return -voltage

    # ------------------------------------------------------------------
    # Smoothing
    # ------------------------------------------------------------------

    @staticmethod
    def smooth(voltage: np.ndarray, window: int = 5) -> np.ndarray:
        """
        Uniform (boxcar) smoothing via scipy.ndimage.uniform_filter1d.
        Uses the same function as the pressure page chart smoothing.

        Parameters
        ----------
        voltage : ndarray
        window : int
            Smoothing window in samples. Forced to an odd integer ≥ 1.
        """
        w = max(1, window | 1)  # ensure odd
        return uniform_filter1d(voltage, size=w)

    # ------------------------------------------------------------------
    # Quality checks
    # ------------------------------------------------------------------

    @staticmethod
    def is_clipped(
        voltage: np.ndarray,
        voltage_range_v: float,
        threshold: float = 0.95,
    ) -> bool:
        """
        Return True if any sample magnitude exceeds ``threshold * voltage_range_v``.
        Indicates ADC saturation / overload.
        """
        limit = threshold * voltage_range_v
        return bool(np.any(np.abs(voltage) >= limit))

    @staticmethod
    def noise_rms(voltage: np.ndarray, baseline_mean: float) -> float:
        """
        RMS of (voltage - baseline_mean) over the full trace.
        A proxy for the noise floor when no signal is present.
        """
        return float(np.sqrt(np.mean((voltage - baseline_mean) ** 2)))
