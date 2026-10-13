"""
noise_stats.py
---------------
Pure statistics functions for the noise-calibration widget (see
docs/runt_trigger_plan.md): measure background noise under the ratemeter's
current channel settings and suggest a runt/edge threshold that avoids
triggering on noise.

No Qt, no hardware access, no file I/O — fully unit-testable offline on any
array. Deliberately independent of WaveformProcessor: that class's
estimate_baseline()/noise_rms() assume the first `fraction` of a trace is a
quiet pre-signal baseline, which does not hold for a noise-only capture where
the whole trace is the measurement.

robust_sigma_mad() ports the block-median-of-MAD pattern already used (for a
similar purpose) in survey_signals.estimate_noise(): pulses/outliers occupy
only a few blocks, so taking the median of each block's own MAD-sigma ignores
them without assuming a particular noise distribution.
"""
from __future__ import annotations

import numpy as np


def rms(voltage: np.ndarray) -> float:
    """RMS about the mean, in the same units as voltage."""
    voltage = np.asarray(voltage, dtype=np.float64)
    return float(np.sqrt(np.mean((voltage - voltage.mean()) ** 2)))


def peak_to_peak(voltage: np.ndarray) -> float:
    """Max - min, in the same units as voltage."""
    voltage = np.asarray(voltage, dtype=np.float64)
    return float(voltage.max() - voltage.min())


def robust_sigma_mad(voltage: np.ndarray, n_blocks: int = 10) -> float:
    """
    Robust noise sigma: median over blocks of each block's MAD-sigma, with
    each block's own median subtracted first (a light detrend) so slow
    baseline wander doesn't inflate the estimate the way plain RMS can.
    """
    voltage = np.asarray(voltage, dtype=np.float64)
    n = len(voltage)
    if n < n_blocks * 10:
        return float(1.4826 * np.median(np.abs(voltage - np.median(voltage))))

    edges = np.linspace(0, n, n_blocks + 1, dtype=int)
    sigmas = []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = voltage[a:b]
        sigmas.append(1.4826 * np.median(np.abs(seg - np.median(seg))))
    return float(np.median(sigmas))


def suggested_threshold(sigma_robust: float, k: float = 5.0) -> float:
    """Suggested trigger threshold magnitude: k standard deviations above noise."""
    return k * sigma_robust


def crossing_rate_hz(voltage: np.ndarray, threshold: float, sample_interval_ns: int) -> float:
    """
    Brute-force count of threshold crossings (|voltage| going from below to
    at-or-above the magnitude threshold), per second. No Gaussian assumption.
    """
    voltage = np.asarray(voltage, dtype=np.float64)
    n = len(voltage)
    if n < 2 or threshold <= 0 or sample_interval_ns <= 0:
        return 0.0

    above = np.abs(voltage) >= threshold
    # Rising edges of `above`: False -> True transitions.
    crossings = int(np.count_nonzero(above[1:] & ~above[:-1]))
    duration_s = n * sample_interval_ns * 1e-9
    return crossings / duration_s if duration_s > 0 else 0.0
