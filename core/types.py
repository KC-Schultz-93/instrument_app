"""
Shared lightweight types and quality labels for events and ions.

These types are UI- and storage-friendly and avoid tight coupling
between pages and processing internals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List


# Event classification (per-frame)
EVENT_EMPTY = "empty"
EVENT_SINGLE = "single"
EVENT_MULTI = "multi"
EVENT_AMBIGUOUS = "ambiguous"

# Per-ion quality flags
ION_OK = "ok"
ION_LOW_SNR = "low_snr"
ION_OVERLAP = "overlap"
ION_UNUSABLE = "unusable"


@dataclass
class EventResult:
    """Per-frame summary and quality.

    - cls: one of the EVENT_* constants
    - n_peaks: total peaks detected in the spectrum (pre-filter)
    - n_usable: number of peaks considered usable (post-overlap/quality)
    - reason: optional short reason for ambiguity/unusable state
    - timestamp: seconds since epoch
    """
    cls: str
    n_peaks: int
    n_usable: int
    reason: Optional[str]
    timestamp: float


@dataclass
class IonView:
    """Per-ion view with physics results and quality flags.

    - f_hz, amp: spectral peak frequency and amplitude
    - E_ev_per_z: per-ion kinetic energy per charge (estimated)
    - V_volts: trap voltage used for calibration
    - mz: calibrated m/z (Th)
    - z: charge state (from injected charge calibration); None if unknown
    - m_amu: mass in amu; None if charge unknown
    - quality: ION_* label describing suitability of this ion
    - snr_db, fwhm_hz: optional peak quality descriptors
    """
    f_hz: float
    amp: float
    E_ev_per_z: Optional[float]
    V_volts: float
    mz: Optional[float]
    z: Optional[float]
    m_amu: Optional[float]
    quality: str = ION_OK
    snr_db: Optional[float] = None
    fwhm_hz: Optional[float] = None
    r2_over_r1: Optional[float] = None
    r3_over_r1: Optional[float] = None


@dataclass
class FrameBlock:
    """Raw acquisition block with sampling metadata.

    - x_i16: numpy int16 array of samples
    - fs_hz: sample rate in Hz
    - timestamp: seconds since epoch (producer clock)
    """
    x_i16: object
    fs_hz: float
    timestamp: float
