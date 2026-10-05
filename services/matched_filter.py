"""
matched_filter.py
-----------------
Matched filter detection of bipolar image charge pulses.

Stateless between traces — instantiate once per run and reuse.
No Qt, no hardware access, no file I/O beyond loading an optional empirical
template at construction time.

Detection pipeline per waveform:
  1. Build (or load) a bipolar template, then stretch/compress it for every
     entry in config.scale_factors (done once, at construction).
  2. For each scale: normalized cross-correlate the template against the waveform.
  3. Find peaks in the correlation output above threshold.
  4. Validate each hit by measuring its peak-to-peak duration, which must fall
     within [pulse_min_us, pulse_max_us] multiplied by that scale.
  5. Merge hits found by more than one scale (best correlation score wins) and
     return BipolarEventRecord list with amplitude read from the raw waveform.

"Pulse duration" everywhere in this module means the peak-to-peak time of the
bipolar pulse (positive lobe extremum to negative lobe extremum), i.e. half the
period of the equivalent sine cycle.
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.ndimage import zoom as ndimage_zoom
from scipy.signal import correlate, find_peaks

from instrument_app.services.daq_models import BipolarEventRecord, MatchedFilterConfig

# (polarity label, template array) pairs checked for one scale factor.
_TemplateVariants = List[Tuple[str, np.ndarray]]


def _normalize_template(template: np.ndarray) -> np.ndarray:
    """Return a zero-mean, unit-max-amplitude float64 copy."""
    out = np.asarray(template, dtype=np.float64)
    out = out - out.mean()
    peak = np.max(np.abs(out))
    if peak > 0:
        out = out / peak
    return out


def _peak_to_peak_samples(template: np.ndarray) -> float:
    """
    Distance in samples between the centres of the positive and negative lobes.

    Empirical templates have broad, slightly noisy lobe tops, so a plain
    argmax/argmin can wander by several samples. Each lobe centre is taken as
    the centroid of the samples within 10% of that lobe's extremum instead.
    """
    def lobe_centre(sign: float) -> float:
        signed = sign * template
        near_top = np.flatnonzero(signed >= 0.9 * signed.max())
        return float(near_top.mean())

    return abs(lobe_centre(1.0) - lobe_centre(-1.0))


class MatchedFilter:
    """Matched filter detector for bipolar image charge pulses."""

    def __init__(self, config: MatchedFilterConfig, sample_interval_ns: int) -> None:
        if not config.scale_factors:
            raise ValueError("MatchedFilterConfig.scale_factors must contain at least one value.")
        if any(s <= 0 for s in config.scale_factors):
            raise ValueError("MatchedFilterConfig.scale_factors must all be positive.")

        self._config = config
        self._sample_interval_ns = sample_interval_ns
        self._last_correlation: Optional[np.ndarray] = None

        base_template = self._load_empirical_template() if config.use_empirical_template else None
        if base_template is None:
            base_template = self._build_synthetic_template(config.pulse_duration_us, sample_interval_ns)

        # Built once here, not per trace: one set of variants per scale factor.
        self._templates: List[Tuple[float, _TemplateVariants]] = []
        for scale in config.scale_factors:
            scaled = self._scale_template(base_template, scale)
            self._templates.append((scale, self._polarity_variants(scaled)))

    @property
    def last_correlation(self) -> Optional[np.ndarray]:
        """Normalized correlation from the most recent detect() call (max across scales)."""
        return self._last_correlation

    # ------------------------------------------------------------------
    # Template construction
    # ------------------------------------------------------------------

    def _build_synthetic_template(self, pulse_duration_us: float, sample_interval_ns: int) -> np.ndarray:
        """
        Single-cycle sine template, positive lobe first.

        The peak-to-peak time of a sine cycle is half its period, so the cycle
        is built twice as long as pulse_duration_us.
        """
        n = max(2, int(2 * pulse_duration_us * 1_000 / sample_interval_ns))
        t = np.linspace(0, 2 * np.pi, n, endpoint=False)
        return _normalize_template(np.sin(t))

    def _load_empirical_template(self) -> Optional[np.ndarray]:
        """
        Load the .npy template, orient it positive-lobe-first, and resample it
        to the acquisition sample interval.

        The .npy file carries no sample interval, so the template's own
        peak-to-peak (measured in samples) is taken to span
        config.pulse_duration_us. Returns None (with a warning) if the file is
        unusable, so the caller can fall back to the synthetic template.
        """
        path_str = self._config.empirical_template_path
        path = Path(path_str) if path_str else None
        if path is None or not path.exists() or path.suffix != ".npy":
            warnings.warn(
                f"Empirical template path {path_str!r} does not exist or is not a "
                ".npy file — falling back to the synthetic template."
            )
            return None

        raw = _normalize_template(np.load(path))
        if raw.ndim != 1 or _peak_to_peak_samples(raw) < 1:
            warnings.warn(
                f"Empirical template {path_str!r} has no usable bipolar shape — "
                "falling back to the synthetic template."
            )
            return None

        if np.argmax(raw) > np.argmin(raw):
            raw = -raw  # store positive-first; polarity variants flip it back as needed

        native_dt_ns = self._config.pulse_duration_us * 1_000 / _peak_to_peak_samples(raw)
        resample_factor = native_dt_ns / self._sample_interval_ns
        return _normalize_template(ndimage_zoom(raw, resample_factor, order=3))

    def _scale_template(self, template: np.ndarray, scale: float) -> np.ndarray:
        """
        Stretch (scale > 1) or compress (scale < 1) a template by resampling.
        Returns a zero-mean, unit-max copy at the new length.
        """
        if abs(scale - 1.0) < 1e-6:
            return template.copy()
        return _normalize_template(ndimage_zoom(template, scale, order=3))

    def _polarity_variants(self, positive_first: np.ndarray) -> _TemplateVariants:
        polarity = self._config.polarity
        if polarity == "negative_first":
            return [("negative_first", -positive_first)]
        if polarity == "both":
            return [("positive_first", positive_first), ("negative_first", -positive_first)]
        return [("positive_first", positive_first)]

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
    # Pulse duration (peak-to-peak) measurement
    # ------------------------------------------------------------------

    def _measure_pulse_duration(
        self,
        waveform: np.ndarray,
        peak_index: int,
        template_len: int,
    ) -> float:
        """
        Measure the peak-to-peak duration (µs) of a bipolar pulse at peak_index.

        The correlation peak sits at the pulse's mid-cycle crossing, so both
        lobe extrema fall within the template-length window centred on it. The
        window is kept at roughly one template length (not wider) so noise
        outside the pulse itself isn't mistaken for a lobe extremum.

        Light smoothing (quarter-template-length boxcar) stops single-sample
        noise spikes from winning the argmax/argmin over the real lobe extrema.
        """
        start = max(0, peak_index - template_len // 2)
        end = min(len(waveform), start + template_len)
        window = waveform[start:end]

        if len(window) < 2:
            return float("nan")

        smooth_window = max(1, (template_len // 4) | 1)
        smoothed = uniform_filter1d(window, size=smooth_window)

        pos_idx = int(np.argmax(smoothed))
        neg_idx = int(np.argmin(smoothed))
        if pos_idx == neg_idx:
            return float("nan")

        return abs(pos_idx - neg_idx) * self._sample_interval_ns / 1_000

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

    def _min_spacing_us(self, scale: float) -> float:
        """
        Minimum separation between distinct events at this scale.

        Never less than 1.5 pulse durations: the opposite-polarity template
        (polarity="both") correlates with a real pulse one pulse duration to
        either side, and those side hits are not separate events.
        """
        return max(self._config.min_distance_us, 1.5 * self._config.pulse_duration_us * scale)

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
            Detected events sorted by time, validated by pulse duration. An
            event seen at several scales is reported once, at its best score.
        """
        cfg = self._config
        n = len(voltage)

        candidates: List[BipolarEventRecord] = []
        combined_all: Optional[np.ndarray] = None

        for scale, variants in self._templates:
            template_len = len(variants[0][1])
            correlations = np.vstack([self._normalized_correlate(voltage, t) for _, t in variants])
            combined = correlations.max(axis=0)
            winner = correlations.argmax(axis=0)

            combined_all = combined if combined_all is None else np.maximum(combined_all, combined)

            distance = max(1, int(self._min_spacing_us(scale) * 1_000 / self._sample_interval_ns))
            indices, _ = find_peaks(combined, height=cfg.correlation_threshold, distance=distance)
            duration_min_us = cfg.pulse_min_us * scale
            duration_max_us = cfg.pulse_max_us * scale

            for idx in indices:
                idx = int(idx)
                if idx < template_len or idx >= n - template_len:
                    continue  # unreliable normalization near trace edges

                duration_us = self._measure_pulse_duration(voltage, idx, template_len)
                if np.isnan(duration_us) or not (duration_min_us <= duration_us <= duration_max_us):
                    continue

                candidates.append(BipolarEventRecord(
                    event_index=idx,
                    time_us=float(time_ns[idx]) / 1_000.0,
                    amplitude_v=self._extract_amplitude(voltage, idx, template_len),
                    correlation_score=float(combined[idx]),
                    period_us=duration_us,
                    polarity=variants[int(winner[idx])][0],
                    matched_scale=scale,
                ))

        self._last_correlation = combined_all

        candidates.sort(key=lambda e: e.time_us)
        events: List[BipolarEventRecord] = []
        for cand in candidates:
            spacing_us = self._min_spacing_us(min(cand.matched_scale, events[-1].matched_scale)) if events else 0.0
            if events and (cand.time_us - events[-1].time_us) < spacing_us:
                if cand.correlation_score > events[-1].correlation_score:
                    events[-1] = cand
            else:
                events.append(cand)
        return events
