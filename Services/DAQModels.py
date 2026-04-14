"""
DAQModels.py
------------
Structured data containers shared across all DAQ modules.
No Qt, no hardware, no file I/O — pure data definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Acquisition configuration
# ---------------------------------------------------------------------------

@dataclass
class AcquisitionConfig:
    """Parameters that define a single block-mode acquisition."""

    channel: str                        # "A" or "B"
    voltage_range_v: float              # e.g. 2.0 for ±2 V
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
