"""
DAQModels.py
------------
Structured data containers shared across all DAQ modules.
No Qt, no hardware, no file I/O â€” pure data definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import numpy as np


# ---------------------------------------------------------------------------
# Acquisition configuration
# ---------------------------------------------------------------------------

@dataclass
class AcquisitionConfig:
    """Parameters that define a single block-mode acquisition."""

    channel: str                        # "A" or "B"
    voltage_range_v: float              # e.g. 2.0 for Â±2 V
    coupling: str                       # "DC" or "AC"
    sample_interval_ns: int             # integer nanoseconds (ps4000 native unit)
    num_samples: int                    # total samples per trace

    trigger_enabled: bool = False
    trigger_threshold_v: Optional[float] = None
    trigger_direction: str = "RISING"   # "RISING" or "FALLING"
    pre_trigger_samples: int = 0        # samples captured before trigger point

    invert_polarity: bool = False       # negate voltage after ADC conversion


# ---------------------------------------------------------------------------
# Raw waveform record
# ---------------------------------------------------------------------------

@dataclass
class WaveformRecord:
    """One acquired waveform trace, in physical units."""

    trace_id: int
    run_id: str
    timestamp: datetime
    voltage: np.ndarray         # float64, volts, shape (num_samples,)
    time_ns: np.ndarray         # float64, nanoseconds from trigger, shape (num_samples,)
    sample_interval_ns: int     # actual achieved interval (may differ slightly from config)
    config: AcquisitionConfig
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-peak record
# ---------------------------------------------------------------------------

@dataclass
class PeakRecord:
    """A single detected peak within a waveform trace."""

    peak_index: int             # sample index within the trace
    time_ns: float              # time from trace start, nanoseconds
    amplitude_v: float          # peak height above baseline, volts


# ---------------------------------------------------------------------------
# Reduced event summary
# ---------------------------------------------------------------------------

@dataclass
class FFTResult:
    """Frequency-domain representation of one waveform trace."""

    frequencies_hz: np.ndarray      # shape (N//2 + 1,)
    magnitudes_db: np.ndarray       # amplitude spectrum in dBV, same shape
    dominant_freq_hz: float         # largest non-DC peak frequency
    dominant_mag_db: float          # magnitude at dominant peak


@dataclass
class CDMSConfig:
    """CDMS physics calibration parameters, configurable per run."""

    charge_cal_uv_per_e: float = 0.64   # Î¼V of signal per elementary charge (CoolFET)
    trap_k_Da_Hz2: float = 0.0          # m/z = K / fÂ²; 0 means uncalibrated (disabled)
    stft_nperseg: int = 512             # STFT window size in samples


@dataclass
class STFTResult:
    """Short-Time Fourier Transform output for one waveform trace."""

    times_s: np.ndarray              # shape (T,) â€” window center times in seconds
    frequencies_hz: np.ndarray       # shape (F,) â€” frequency bins in Hz
    power_db: np.ndarray             # shape (F, T) â€” magnitude spectrum in dBV
    dominant_freq_track_hz: np.ndarray  # shape (T,) â€” per-window dominant frequency


@dataclass
class CDMSResult:
    """Per-trace CDMS physics result derived from FFT and signal amplitude."""

    oscillation_freq_hz: float
    charge_e: float
    mz_Da_per_e: Optional[float]    # None when K_trap is uncalibrated
    mass_Da: Optional[float]        # None when K_trap is uncalibrated


@dataclass
class EventSummary:
    """
    Reduced feature summary for one processed trace.
    Written to the per-run CSV for later filtering and re-analysis.
    """

    trace_id: int
    run_id: str
    timestamp: datetime
    accepted: bool
    classification: str         # see EventDetector module constants

    baseline_mean_v: float
    baseline_rms_v: float
    signal_max_v: float
    signal_min_v: float
    num_peaks: int

    mean_peak_height_v: Optional[float] = None
    mean_peak_spacing_ns: Optional[float] = None
    notes: str = ""

    # CDMS physics â€” populated when CDMSAnalyzer runs
    oscillation_freq_hz: Optional[float] = None
    charge_e: Optional[float] = None
    mz_Da_per_e: Optional[float] = None
    mass_Da: Optional[float] = None


# ---------------------------------------------------------------------------
# Ratemeter configuration
# ---------------------------------------------------------------------------

@dataclass
class AmplitudeBand:
    """One user-defined amplitude window for ratemeter counting."""

    label: str          # e.g. "Band 1", "Band 2"
    low_mv: float        # lower bound, millivolts (inclusive)
    high_mv: float       # upper bound, millivolts (inclusive)
    color: str           # hex color string, e.g. "#4fc3f7"


@dataclass
class RatemeterConfig:
    """Full configuration for one ratemeter run."""

    channel: str
    voltage_range_v: float
    coupling: str
    sample_interval_ns: int
    window_duration_ms: float
    rate_averaging_s: float
    bands: List[AmplitudeBand]

    @property
    def num_samples(self) -> int:
        return max(1, int(self.window_duration_ms * 1e6 / self.sample_interval_ns))

    def to_acquisition_config(
        self,
        trigger_enabled: bool = False,
        trigger_threshold_v: float = 0.0,
        trigger_direction: str = "RISING",
    ) -> "AcquisitionConfig":
        return AcquisitionConfig(
            channel=self.channel,
            voltage_range_v=self.voltage_range_v,
            coupling=self.coupling,
            sample_interval_ns=self.sample_interval_ns,
            num_samples=self.num_samples,
            trigger_enabled=trigger_enabled,
            trigger_threshold_v=trigger_threshold_v,
            trigger_direction=trigger_direction,
        )
