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
from scipy.signal import stft as scipy_stft

from Services.DAQModels import STFTResult


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

    # ------------------------------------------------------------------
    # Frequency domain
    # ------------------------------------------------------------------

    @staticmethod
    def compute_fft(
        voltage: np.ndarray,
        sample_interval_ns: float,
    ) -> tuple:
        """
        Compute the one-sided amplitude spectrum of a voltage trace.

        A Hann window is applied before the FFT to reduce spectral leakage.
        The DC bin is preserved but not doubled so it represents true DC level.

        Parameters
        ----------
        voltage : ndarray
            Voltage array in volts (baseline-subtracted recommended).
        sample_interval_ns : float
            Sample interval in nanoseconds.

        Returns
        -------
        frequencies_hz : ndarray
            Frequency axis, shape (N//2 + 1,), in Hz.
        magnitudes_db : ndarray
            Amplitude spectrum in dBV, same shape.
        dominant_freq_hz : float
            Frequency of the largest non-DC spectral peak (Hz).
            Returns 0.0 if the spectrum is flat or the trace is trivially short.
        dominant_mag_db : float
            Magnitude at dominant_freq_hz in dBV.
        """
        n = len(voltage)
        if n < 4:
            empty = np.zeros(1)
            return empty, empty, 0.0, -200.0

        window = np.hanning(n)
        spectrum = np.fft.rfft(voltage * window)

        sample_interval_s = sample_interval_ns * 1e-9
        frequencies_hz = np.fft.rfftfreq(n, d=sample_interval_s)

        # Normalize: two-sided → one-sided, scale by window sum for amplitude
        # correction so the result is in volts peak (then converted to dBV).
        win_sum = window.sum()
        amplitudes = (2.0 / win_sum) * np.abs(spectrum)
        amplitudes[0] /= 2.0   # DC bin is not doubled in the one-sided spectrum

        magnitudes_db = 20.0 * np.log10(np.maximum(amplitudes, 1e-12))

        # Dominant frequency: largest bin excluding DC (index 0)
        if len(magnitudes_db) > 1:
            peak_idx = int(np.argmax(magnitudes_db[1:])) + 1
            dominant_freq_hz = float(frequencies_hz[peak_idx])
            dominant_mag_db = float(magnitudes_db[peak_idx])
        else:
            dominant_freq_hz = 0.0
            dominant_mag_db = float(magnitudes_db[0])

        return frequencies_hz, magnitudes_db, dominant_freq_hz, dominant_mag_db

    @staticmethod
    def compute_stft(
        voltage: np.ndarray,
        sample_interval_ns: float,
        nperseg: int = 512,
    ) -> STFTResult:
        """
        Compute the Short-Time Fourier Transform of a voltage trace.

        Uses a Hann window with 50% overlap. The result is a 2D power array
        (dBV) that can be displayed as a spectrogram to observe frequency
        evolution over time — useful for detecting mid-trace mass/charge changes.

        Parameters
        ----------
        voltage : ndarray
            Baseline-subtracted voltage array in volts.
        sample_interval_ns : float
            Sample interval in nanoseconds.
        nperseg : int
            Samples per STFT window. Controls time-frequency resolution tradeoff.
            Must be ≤ len(voltage); clamped automatically.

        Returns
        -------
        STFTResult
            times_s          : shape (T,)    — window center times in seconds
            frequencies_hz   : shape (F,)    — frequency bins in Hz
            power_db         : shape (F, T)  — magnitude spectrum in dBV
            dominant_freq_track_hz : shape (T,) — dominant non-DC freq per window
        """
        n = len(voltage)
        if n < 4:
            empty_f = np.zeros(1)
            empty_t = np.zeros(1)
            return STFTResult(
                times_s=empty_t,
                frequencies_hz=empty_f,
                power_db=np.zeros((1, 1)),
                dominant_freq_track_hz=empty_t,
            )

        fs = 1.0 / (sample_interval_ns * 1e-9)  # sample rate in Hz
        seg = min(nperseg, n)
        noverlap = seg // 2

        freqs, times, Zxx = scipy_stft(
            voltage,
            fs=fs,
            window="hann",
            nperseg=seg,
            noverlap=noverlap,
        )

        amplitudes = np.abs(Zxx)
        power_db = 20.0 * np.log10(np.maximum(amplitudes, 1e-12))

        # Per-window dominant frequency: argmax of amplitude excluding DC (row 0)
        if power_db.shape[0] > 1:
            dominant_idx = np.argmax(amplitudes[1:, :], axis=0) + 1
        else:
            dominant_idx = np.zeros(power_db.shape[1], dtype=int)
        dominant_freq_track_hz = freqs[dominant_idx]

        return STFTResult(
            times_s=times,
            frequencies_hz=freqs,
            power_db=power_db,
            dominant_freq_track_hz=dominant_freq_track_hz,
        )
