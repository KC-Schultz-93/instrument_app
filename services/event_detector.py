"""
EventDetector.py
----------------
Rule-based trace classification for DAQ waveforms.

Classification labels are module-level string constants so that other modules
can import and compare against them without magic strings.

Rules are applied in priority order; the first matching rule wins.

No Qt, no file I/O, no hardware access.
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np

from instrument_app.services.daq_models import EventSummary, PeakRecord, WaveformRecord
from instrument_app.services.waveform_processor import WaveformProcessor


# ---------------------------------------------------------------------------
# Classification label constants
# ---------------------------------------------------------------------------

CLASS_EMPTY      = "empty"          # no signal above noise threshold
CLASS_NOISY      = "noisy"          # baseline noise too high to classify
CLASS_CLIPPED    = "overload"       # ADC saturation detected
CLASS_POSSIBLE   = "possible_event" # one or more peaks but not clearly periodic
CLASS_LIKELY_ION = "likely_trapped_ion"  # multiple regularly-spaced peaks
CLASS_AMBIGUOUS  = "ambiguous"      # doesn't fit any other category


class EventDetector:
    """
    Classifies a waveform trace based on simple, inspectable rules.

    Parameters
    ----------
    noise_threshold_v : float
        Baseline RMS above which the trace is classified as "noisy".
    signal_threshold_sigma : float
        Number of baseline-RMS sigmas the signal must exceed to be considered
        non-empty.  Default 3.0.
    min_peaks_for_ion : int
        Minimum peak count to consider a trace "likely_trapped_ion".
    spacing_variation_limit : float
        Coefficient of variation (std/mean) of peak spacings below which a
        multi-peak trace is classified as "likely_trapped_ion".
    """

    def __init__(
        self,
        noise_threshold_v: float = 0.005,
        signal_threshold_sigma: float = 3.0,
        min_peaks_for_ion: int = 2,
        spacing_variation_limit: float = 0.15,
    ) -> None:
        self.noise_threshold_v = noise_threshold_v
        self.signal_threshold_sigma = signal_threshold_sigma
        self.min_peaks_for_ion = min_peaks_for_ion
        self.spacing_variation_limit = spacing_variation_limit

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify(
        self,
        record: WaveformRecord,
        peaks: List[PeakRecord],
        baseline_mean: float = 0.0,
        baseline_rms: float = 0.0,
    ) -> Tuple[str, bool]:
        """
        Classify a waveform trace.

        Parameters
        ----------
        record : WaveformRecord
            The raw (or baseline-subtracted) waveform.
        peaks : list[PeakRecord]
            Peaks found by SignalExtractor.find_peaks().
        baseline_mean : float
            Pre-computed baseline mean (volts).
        baseline_rms : float
            Pre-computed baseline RMS (volts).

        Returns
        -------
        (classification_label, accepted)
            accepted is True for CLASS_POSSIBLE and CLASS_LIKELY_ION.
        """
        voltage = record.voltage
        voltage_range_v = record.config.voltage_range_v

        # --- Rule 1: overload / clipping ---
        if WaveformProcessor.is_clipped(voltage, voltage_range_v):
            return CLASS_CLIPPED, False

        # Recompute baseline if caller didn't supply it
        if baseline_mean == 0.0 and baseline_rms == 0.0:
            try:
                baseline_mean, baseline_rms = WaveformProcessor.estimate_baseline(voltage)
            except ValueError:
                return CLASS_AMBIGUOUS, False

        # --- Rule 2: empty trace (signal below threshold) ---
        signal_peak = float(np.max(np.abs(voltage - baseline_mean)))
        threshold = self.signal_threshold_sigma * baseline_rms
        if signal_peak < threshold:
            return CLASS_EMPTY, False

        # --- Rule 3: noisy baseline ---
        if baseline_rms > self.noise_threshold_v:
            return CLASS_NOISY, False

        # --- Rule 4: likely trapped ion (multiple periodic peaks) ---
        num_peaks = len(peaks)
        if num_peaks >= self.min_peaks_for_ion:
            spacings = self._peak_spacings(peaks)
            if spacings is not None and len(spacings) > 0:
                cv = self._spacing_variation(spacings)
                if cv < self.spacing_variation_limit:
                    return CLASS_LIKELY_ION, True

        # --- Rule 5: possible event (at least one peak) ---
        if num_peaks >= 1:
            return CLASS_POSSIBLE, True

        # --- Rule 6: no peaks found â€” classify as empty regardless of raw max ---
        # The peak finder uses height + prominence + distance filters; if it found
        # nothing, any high raw sample is just a noise fluctuation.
        return CLASS_EMPTY, False

    # ------------------------------------------------------------------
    # Internal helpers (kept here to avoid a circular import with
    # SignalExtractor â€” these are tiny and duplication is acceptable)
    # ------------------------------------------------------------------

    @staticmethod
    def _peak_spacings(peaks: List[PeakRecord]):
        if len(peaks) < 2:
            return None
        times = np.array([p.time_ns for p in peaks])
        return np.diff(times)

    @staticmethod
    def _spacing_variation(spacings: np.ndarray) -> float:
        if len(spacings) < 2:
            return 0.0
        mean = spacings.mean()
        if mean == 0.0:
            return 0.0
        return float(spacings.std() / mean)
