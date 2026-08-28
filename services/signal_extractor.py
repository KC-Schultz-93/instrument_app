"""
SignalExtractor.py
------------------
Peak finding and signal feature extraction for processed waveform traces.

Stateless between traces â€” instantiate once per run and reuse.
No Qt, no file I/O, no hardware access.

Depends on scipy.signal.find_peaks (already a project dependency via
WaveformProcessor's use of scipy.ndimage).
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
from scipy.signal import find_peaks, peak_widths

from instrument_app.services.daq_models import PeakRecord


class SignalExtractor:
    """
    Peak finding and feature extraction.

    Parameters
    ----------
    min_height_sigma : float
        Minimum peak height expressed as multiples of the baseline RMS above
        the baseline mean.  Default 3.0 (3-sigma threshold).
    min_distance_samples : int
        Minimum separation between detected peaks in samples.
    min_prominence_v : float
        Minimum peak prominence in volts (helps reject noise shoulders).
    """

    def __init__(
        self,
        min_height_sigma: float = 3.0,
        min_distance_samples: int = 50,
        min_prominence_v: float = 0.001,
    ) -> None:
        self.min_height_sigma = min_height_sigma
        self.min_distance_samples = min_distance_samples
        self.min_prominence_v = min_prominence_v

    # ------------------------------------------------------------------
    # Peak detection
    # ------------------------------------------------------------------

    def find_peaks(
        self,
        voltage: np.ndarray,
        baseline_mean: float,
        baseline_rms: float,
        time_ns: np.ndarray,
        height_threshold_v: Optional[float] = None,
        width_rel_height: float = 0.5,
    ) -> List[PeakRecord]:
        """
        Find peaks in the (baseline-corrected) voltage trace.

        Parameters
        ----------
        voltage : ndarray
            Voltage array, ideally baseline-subtracted.
        baseline_mean : float
            Baseline mean (used to compute absolute height threshold).
        baseline_rms : float
            Baseline RMS (used to compute sigma-based threshold).
        time_ns : ndarray
            Time array in nanoseconds, same length as voltage.
        height_threshold_v : float, optional
            Explicit height threshold in volts.  When provided, overrides the
            sigma-based rule (min_height_sigma * baseline_rms).  Use this when
            the caller knows the minimum amplitude of interest directly (e.g.
            the lowest band lower bound) rather than deriving it from noise RMS.
        width_rel_height : float, optional
            Fractional height (relative to peak prominence) at which to measure
            peak width via scipy.signal.peak_widths. Default 0.5 (FWHM).

        Returns
        -------
        List[PeakRecord]
            Peaks sorted by time (ascending).
        """
        if height_threshold_v is not None:
            height_threshold = height_threshold_v
        else:
            # voltage is baseline-corrected (mean ≈ 0), so threshold is relative to 0
            height_threshold = self.min_height_sigma * baseline_rms

        indices, properties = find_peaks(
            voltage,
            height=height_threshold,
            distance=self.min_distance_samples,
            prominence=self.min_prominence_v,
        )

        # Width measurement — uses scipy peak_widths on the same voltage array
        sample_interval_ns = float(time_ns[1] - time_ns[0]) if len(time_ns) > 1 else 1.0
        if len(indices) > 0:
            widths_samp, _, left_ips, right_ips = peak_widths(
                voltage,
                indices,
                rel_height=width_rel_height,
            )
        else:
            widths_samp = left_ips = right_ips = np.array([])

        peaks = []
        for i, idx in enumerate(indices):
            if i < len(widths_samp):
                w_samp = float(widths_samp[i])
                w_ns = w_samp * sample_interval_ns
                l_idx = max(0, min(int(round(float(left_ips[i]))), len(time_ns) - 1))
                r_idx = max(0, min(int(round(float(right_ips[i]))), len(time_ns) - 1))
                rise_ns = float(time_ns[l_idx])
                fall_ns = float(time_ns[r_idx])
            else:
                w_samp = w_ns = rise_ns = fall_ns = None

            peaks.append(
                PeakRecord(
                    peak_index=int(idx),
                    time_ns=float(time_ns[idx]),
                    amplitude_v=float(voltage[idx]),  # already baseline-corrected
                    width_samples=w_samp,
                    width_ns=w_ns,
                    rise_ns=rise_ns,
                    fall_ns=fall_ns,
                )
            )

        # sort by time (should already be sorted, but be explicit)
        peaks.sort(key=lambda p: p.time_ns)
        return peaks

    # ------------------------------------------------------------------
    # Spacing analysis
    # ------------------------------------------------------------------

    @staticmethod
    def peak_spacings(peaks: List[PeakRecord]) -> Optional[np.ndarray]:
        """
        Compute inter-peak time spacings in nanoseconds.

        Returns None if fewer than two peaks are present.
        """
        if len(peaks) < 2:
            return None
        times = np.array([p.time_ns for p in peaks])
        return np.diff(times)

    @staticmethod
    def spacing_variation(spacings: np.ndarray) -> float:
        """
        Coefficient of variation (std / mean) of inter-peak spacings.
        A low value (< ~0.15) indicates a periodic, regularly-spaced signal
        consistent with a trapped ion signature.
        Returns 0.0 for a single spacing (no variation possible).
        """
        if len(spacings) < 2:
            return 0.0
        mean = spacings.mean()
        if mean == 0.0:
            return 0.0
        return float(spacings.std() / mean)

    # ------------------------------------------------------------------
    # Summary statistics
    # ------------------------------------------------------------------

    def summarize(
        self,
        peaks: List[PeakRecord],
        baseline_mean: float,
        baseline_rms: float,
    ) -> dict:
        """
        Compute a summary dict for a list of peaks.

        Keys
        ----
        num_peaks : int
        mean_peak_height_v : float or None
        mean_peak_spacing_ns : float or None
        spacing_variation : float or None
        peak_times_ns : list[float]
        mean_width_ns : float or None
        """
        num_peaks = len(peaks)
        mean_peak_height_v = None
        mean_peak_spacing_ns = None
        cv = None
        peak_times_ns = [p.time_ns for p in peaks]

        if num_peaks > 0:
            mean_peak_height_v = float(
                np.mean([p.amplitude_v for p in peaks])
            )

        spacings = self.peak_spacings(peaks)
        if spacings is not None and len(spacings) > 0:
            mean_peak_spacing_ns = float(spacings.mean())
            cv = self.spacing_variation(spacings)

        widths_ns = [p.width_ns for p in peaks if p.width_ns is not None]
        mean_width_ns = float(np.mean(widths_ns)) if widths_ns else None

        return {
            "num_peaks": num_peaks,
            "mean_peak_height_v": mean_peak_height_v,
            "mean_peak_spacing_ns": mean_peak_spacing_ns,
            "spacing_variation": cv,
            "peak_times_ns": peak_times_ns,
            "mean_width_ns": mean_width_ns,
        }
