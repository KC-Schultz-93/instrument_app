"""
matched_filter.py
-----------------
Matched filter detection of bipolar image charge pulses.

Stateless between traces — instantiate once per run and reuse.
No Qt, no hardware access, no file I/O beyond loading an optional empirical
template at construction time.

Detection pipeline per waveform:
  1. Generate (or load) a bipolar template.
  2. Normalized cross-correlate the template against the waveform.
  3. Find peaks in the correlation output above threshold.
  4. Validate each hit by measuring its zero-crossing period.
  5. Return BipolarEventRecord list with amplitude read from the raw waveform.
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import List, Optional

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import correlate, find_peaks

from instrument_app.services.daq_models import BipolarEventRecord, MatchedFilterConfig


class MatchedFilter:
    """Matched filter detector for bipolar image charge pulses."""

    def __init__(self, config: MatchedFilterConfig, sample_interval_ns: int) -> None:
        self._config = config
        self._sample_interval_ns = sample_interval_ns
        self._last_correlation: Optional[np.ndarray] = None

        self._template: np.ndarray
        self._template_neg: Optional[np.ndarray] = None

        loaded = False
        if config.use_empirical_template and config.empirical_template_path:
            path = Path(config.empirical_template_path)
            if path.exists() and path.suffix == ".npy":
                raw = np.load(path).astype(np.float64)
                raw = raw - raw.mean()
                max_abs = np.max(np.abs(raw))
                if max_abs > 0:
                    raw = raw / max_abs
                self._template = raw
                if config.polarity == "both":
                    self._template_neg = -raw
                loaded = True
            else:
                warnings.warn(
                    f"Empirical template path {config.empirical_template_path!r} "
                    "does not exist or is not a .npy file — falling back to the "
                    "synthetic template."
                )

        if not loaded:
            base_polarity = "negative_first" if config.polarity == "negative_first" else "positive_first"
            self._template = self._build_synthetic_template(
                config.period_us, sample_interval_ns, base_polarity
            )
            if config.polarity == "both":
                self._template_neg = self._build_synthetic_template(
                    config.period_us, sample_interval_ns, "negative_first"
                )

    @property
    def last_correlation(self) -> Optional[np.ndarray]:
        """Normalized correlation array from the most recent detect() call."""
        return self._last_correlation

    # ------------------------------------------------------------------
    # Template generation
    # ------------------------------------------------------------------

    def _build_synthetic_template(
        self,
        period_us: float,
        sample_interval_ns: int,
        polarity: str,
    ) -> np.ndarray:
        """
        Generate a single-cycle sine wave template.

        Returns a normalized (zero-mean, unit-max) 1-D float64 array.
        """
        n = max(2, int(period_us * 1_000 / sample_interval_ns))
        t = np.linspace(0, 2 * np.pi, n, endpoint=False)
        template = np.sin(t)
        if polarity == "negative_first":
            template = -template
        template = template - template.mean()
        template = template / np.max(np.abs(template))
        return template

    # ------------------------------------------------------------------
    # Normalized cross-correlation
    # ------------------------------------------------------------------

    def _normalized_correlate(
        self,
        waveform: np.ndarray,
        template: np.ndarray,
    ) -> np.ndarray:
        """
        Compute normalized cross-correlation between waveform and template.

        Output range is approximately [-1, 1]. Normalization divides by the
        local RMS of the waveform in a window matching the template length,
        so the score is amplitude-independent.
        """
        n_t = len(template)
        raw_corr = correlate(waveform, template, mode="same")

        waveform_sq = waveform ** 2
        cs = np.cumsum(waveform_sq)
        cs = np.concatenate(([0.0], cs))
        half = n_t // 2
        left = np.maximum(0, np.arange(len(waveform)) - half)
        right = np.minimum(len(waveform), np.arange(len(waveform)) - half + n_t)
        window_energy = cs[right] - cs[left]
        local_rms = np.sqrt(np.maximum(window_energy / n_t, 1e-12))

        template_rms = np.sqrt(np.mean(template ** 2))
        normalized = raw_corr / (local_rms * template_rms * n_t)
        return np.clip(normalized, -1.0, 1.0)

    # ------------------------------------------------------------------
    # Zero-crossing period measurement
    # ------------------------------------------------------------------

    def _measure_period(
        self,
        waveform: np.ndarray,
        peak_index: int,
        template_len: int,
        sample_interval_ns: int,
    ) -> float:
        """
        Measure the period of a bipolar pulse at peak_index.

        A single-cycle bipolar pulse's positive and negative lobe extrema are
        exactly half a period apart. Measuring that separation (and doubling
        it) is equivalent to a zero-crossing period measurement but immune to
        the degenerate case where a flat/low-noise baseline never produces a
        detectable sign change at the pulse's leading or trailing edge —
        which a literal first-to-last zero-crossing count would miss or
        under-measure.

        The correlation peak sits at the pulse's mid-cycle crossing, so both
        lobe extrema fall within one quarter-template-length of it. The
        search window is kept at roughly one template length (not wider) so
        noise outside the pulse itself isn't mistaken for a lobe extremum.
        """
        window_len = template_len
        start = max(0, peak_index - window_len // 2)
        end = min(len(waveform), start + window_len)
        window = waveform[start:end]

        if len(window) < 2:
            return float("nan")

        # Light smoothing (quarter-template-length boxcar) suppresses single-sample
        # noise spikes from winning the argmax/argmin over the pulse's real lobe
        # extrema, which a low-SNR event is otherwise vulnerable to.
        smooth_window = max(1, (template_len // 4) | 1)
        smoothed = uniform_filter1d(window, size=smooth_window)

        pos_idx = int(np.argmax(smoothed))
        neg_idx = int(np.argmin(smoothed))
        if pos_idx == neg_idx:
            return float("nan")

        half_period_samples = abs(pos_idx - neg_idx)
        period_us = 2 * half_period_samples * sample_interval_ns / 1_000
        return period_us

    # ------------------------------------------------------------------
    # Amplitude extraction
    # ------------------------------------------------------------------

    def _extract_amplitude(self, waveform: np.ndarray, peak_index: int, template_len: int) -> float:
        """
        Return the signed amplitude of whichever lobe (positive or negative)
        has the greater magnitude within a half-template-length window
        around peak_index.
        """
        half = max(1, template_len // 2)
        start = max(0, peak_index - half)
        end = min(len(waveform), peak_index + half + 1)
        window = waveform[start:end]
        local_idx = int(np.argmax(np.abs(window)))
        return float(window[local_idx])

    # ------------------------------------------------------------------
    # Main detection method
    # ------------------------------------------------------------------

    def detect(
        self,
        voltage: np.ndarray,
        time_ns: np.ndarray,
    ) -> List[BipolarEventRecord]:
        """
        Run matched filter detection on one baseline-corrected waveform.

        Parameters
        ----------
        voltage : ndarray
            Baseline-corrected voltage array (volts).
        time_ns : ndarray
            Time array in nanoseconds, same length as voltage.

        Returns
        -------
        List[BipolarEventRecord]
            Detected events sorted by time, validated by period.
        """
        template_len = len(self._template)
        corr_pos = self._normalized_correlate(voltage, self._template)

        winner_is_neg = None
        if self._config.polarity == "both" and self._template_neg is not None:
            corr_neg = self._normalized_correlate(voltage, self._template_neg)
            combined = np.maximum(corr_pos, corr_neg)
            winner_is_neg = corr_neg > corr_pos
        else:
            combined = corr_pos

        self._last_correlation = combined

        distance = max(1, int(self._config.min_distance_us * 1_000 / self._sample_interval_ns))
        indices, _ = find_peaks(
            combined,
            height=self._config.correlation_threshold,
            distance=distance,
        )

        n = len(voltage)
        events: List[BipolarEventRecord] = []
        for idx in indices:
            idx = int(idx)
            if idx < template_len or idx >= n - template_len:
                continue  # unreliable normalization near trace edges

            period_us = self._measure_period(voltage, idx, template_len, self._sample_interval_ns)
            if np.isnan(period_us):
                continue
            if not (self._config.period_min_us <= period_us <= self._config.period_max_us):
                continue

            if winner_is_neg is not None:
                polarity = "negative_first" if winner_is_neg[idx] else "positive_first"
            else:
                polarity = self._config.polarity

            amplitude_v = self._extract_amplitude(voltage, idx, template_len)

            events.append(BipolarEventRecord(
                event_index=idx,
                time_us=float(time_ns[idx]) / 1_000.0,
                amplitude_v=amplitude_v,
                correlation_score=float(combined[idx]),
                period_us=period_us,
                polarity=polarity,
            ))

        events.sort(key=lambda e: e.time_us)
        return events
