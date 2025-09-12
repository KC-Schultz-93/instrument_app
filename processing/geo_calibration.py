
"""
Module: instrument_app.processing.geo_calibration
Purpose: Analysis/business-logic layer for CDMS calibration and ion extraction.

Stable API exposed to the rest of the app:
- load_calibration(json_path) -> calibration object (C(E,V) + defaults)
- process_frame(x: np.ndarray, E, V, calib: CDMSCalibration, reject_overlap_hz=None) -> List[Ion]
- Optional helpers: configure_processing(...), update_hist(hist_state, ions), export_hist(hist_state, path)

All physics/signal-processing choices (windowing, peak spacing, overlap rejection,
charge calibration hook) live here. GUI/device code must not leak into this module.
"""

from __future__ import annotations
import json, math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Dict

import numpy as np
from scipy.signal import get_window, find_peaks

E_CHARGE = 1.602176634e-19  # C

# ------------------ Calibration structures ------------------

@dataclass
class CPoly:
    A00: float; A10: float; A01: float; A20: float; A11: float; A02: float
    A30: float; A21: float; A12: float; A03: float

    @classmethod
    def from_json(cls, d: Dict[str, float]) -> "CPoly":
        # Missing keys default to 0 for robustness
        def g(k): return float(d.get(k, 0.0))
        return cls(g("A00"), g("A10"), g("A01"), g("A20"), g("A11"), g("A02"),
                   g("A30"), g("A21"), g("A12"), g("A03"))

    def C(self, E: np.ndarray, V: float) -> np.ndarray:
        # Evaluate √C polynomial then square it
        rootC = (
            self.A00
            + self.A10*E
            + self.A01*V
            + self.A20*(E**2)
            + self.A11*(E*V)
            + self.A02*(V**2)
            + self.A30*(E**3)
            + self.A21*((E**2)*V)
            + self.A12*(E*(V**2))
            + self.A03*(V**3)
        )
        return rootC * rootC

@dataclass
class CDMSCalibration:
    cpoly: CPoly
    voltage_median: Optional[float] = None

    @classmethod
    def load(cls, path: str | Path) -> "CDMSCalibration":
        js = json.loads(Path(path).read_text(encoding="utf-8"))
        # Allow combined summary (from --summary)
        if "files" in js and isinstance(js["files"], list) and js["files"]:
            js = js["files"][0]
        poly = js.get("C_poly", None)
        if not poly:
            raise ValueError("JSON has no C_poly. Run cdms_geometry_fit_v3.py with eV_per_z present.")
        return cls(CPoly.from_json(poly), js.get("voltage_median", None))

# ------------------ Simple FFT + peak pick ------------------

@dataclass
class FFTConfig:
    fs: float                 # sample rate [Hz]
    window: str = "hann"
    nfft: Optional[int] = None
    min_prominence: float = 0.0    # tune for SNR
    min_distance_hz: float = 20.0  # enforce Δf spacing for peaks

@dataclass
class Peak:
    f_hz: float
    amp: float   # spectral amplitude at peak (arbitrary units unless calibrated)

def _fft_peaks(x: np.ndarray, cfg: FFTConfig) -> List[Peak]:
    """
    Compute single-sided magnitude spectrum and pick peaks.
    """
    N = len(x)
    nfft = cfg.nfft or int(2**math.ceil(math.log2(N)))  # zero-pad to next pow2 (optional)
    w = get_window(cfg.window, N, fftbins=True)
    xw = x * w
    X = np.fft.rfft(xw, n=nfft)          # one-sided FFT
    freqs = np.fft.rfftfreq(nfft, d=1.0/cfg.fs)
    mag = np.abs(X)

    # Convert min_distance_hz to bins
    df = freqs[1] - freqs[0] if len(freqs) > 1 else cfg.fs/nfft
    min_distance_bins = max(1, int(round(cfg.min_distance_hz / df)))

    # Peak picking
    idx, props = find_peaks(mag, prominence=cfg.min_prominence, distance=min_distance_bins)
    # Quadratic interpolation around the peak for sub-bin frequency
    peaks: List[Peak] = []
    for i in idx:
        if 1 <= i < len(mag) - 1:
            y0, y1, y2 = mag[i-1], mag[i], mag[i+1]
            denom = (y0 - 2*y1 + y2)
            delta = 0.0 if denom == 0 else 0.5*(y0 - y2)/denom
            f_refined = freqs[i] + delta*df
            amp_refined = y1 - 0.25*(y0 - y2)*delta  # parabola vertex value
        else:
            f_refined = freqs[i]; amp_refined = mag[i]
        peaks.append(Peak(f_refined, amp_refined))
    return peaks

# ------------------ Charge calibration hook ------------------

# User supplies a function that maps (peak amplitude, frequency) -> charge [Coulombs].
# It should include your analog gain, detector responsivity, frequency response, etc.
ChargeCal = Callable[[float, float], float]

def example_charge_cal(amp: float, f_hz: float) -> float:
    """
    Placeholder. Replace with your instrument's amplitude→charge calibration.
    """
    # e.g., q = amp / G(f), with G your system transfer function [A/C or V/C equivalents]
    # Return Coulombs.
    raise NotImplementedError

# ------------------ Main per-frame processor ------------------

@dataclass
class IonResult:
    f_hz: float
    amp: float
    E_ev_per_z: float
    V_volts: float
    mz: float
    z: Optional[float] = None
    m_amu: Optional[float] = None

class RealTimeCDMS:
    def __init__(self,
                 calib: CDMSCalibration,
                 fft_cfg: FFTConfig,
                 charge_cal: Optional[ChargeCal] = None):
        self.calib = calib
        self.fft_cfg = fft_cfg
        self.charge_cal = charge_cal

    def process_frame(self,
                      x: np.ndarray,
                      E_ev_per_z_for_peaks: float | List[float] | np.ndarray,
                      V_volts: Optional[float] = None,
                      reject_overlap_hz: Optional[float] = None) -> List[IonResult]:
        """
        x: 1D time-domain trace from PicoScope (single frame)
        E_ev_per_z_for_peaks: either a single value (assumed for all ions in the frame)
                              or a list/array with one E per detected peak
        V_volts: trap voltage; defaults to calibration.voltage_median if None
        reject_overlap_hz: if set, discard peaks closer than this spacing (post-pick safety)
        """
        V = float(V_volts if V_volts is not None else (self.calib.voltage_median or 0.0))
        if V == 0.0:
            raise ValueError("Trap voltage V is required (either pass V_volts or have voltage_median in JSON).")

        peaks = _fft_peaks(x, self.fft_cfg)
        if not peaks:
            return []

        # Optional second-stage spacing
        if reject_overlap_hz:
            peaks = sorted(peaks, key=lambda p: p.f_hz)
            dedup = [peaks[0]]
            for p in peaks[1:]:
                if abs(p.f_hz - dedup[-1].f_hz) >= reject_overlap_hz:
                    dedup.append(p)
            peaks = dedup

        # Assign E values
        if np.isscalar(E_ev_per_z_for_peaks):
            E = np.full(len(peaks), float(E_ev_per_z_for_peaks), dtype=float)
        else:
            E = np.asarray(E_ev_per_z_for_peaks, dtype=float)
            if E.shape[0] != len(peaks):
                raise ValueError("Length of E_ev_per_z_for_peaks must match number of detected peaks.")

        # Compute m/z using C(E,V)/f^2
        freqs = np.array([p.f_hz for p in peaks], dtype=float)
        Cvals = self.calib.cpoly.C(E, V)
        mz = Cvals / (freqs**2)

        # Charge (optional) and mass
        results: List[IonResult] = []
        for i, p in enumerate(peaks):
            z_val: Optional[float] = None
            m_val: Optional[float] = None
            if self.charge_cal is not None:
                q_coul = self.charge_cal(p.amp, p.f_hz)  # user-defined
                z_val = q_coul / E_CHARGE
                m_val = z_val * mz[i]
            results.append(IonResult(f_hz=p.f_hz, amp=p.amp,
                                     E_ev_per_z=float(E[i]), V_volts=V,
                                     mz=float(mz[i]), z=z_val, m_amu=m_val))
        return results

# ------------------ Public module-level API ------------------

# Type alias for external callers
Ion = IonResult

_default_fft_cfg: Optional[FFTConfig] = None
_default_charge_cal: Optional[ChargeCal] = None

def configure_processing(
    *,
    fs_hz: float,
    window: str = "hann",
    nfft: Optional[int] = None,
    min_prominence: float = 0.0,
    min_distance_hz: float = 20.0,
    charge_cal: Optional[ChargeCal] = None,
) -> None:
    """Set global FFT/peak-pick parameters and optional charge calibration.

    Call this once from the acquisition setup with the actual sample rate.
    """
    global _default_fft_cfg, _default_charge_cal
    _default_fft_cfg = FFTConfig(
        fs=float(fs_hz),
        window=str(window),
        nfft=int(nfft) if nfft is not None else None,
        min_prominence=float(min_prominence),
        min_distance_hz=float(min_distance_hz),
    )
    _default_charge_cal = charge_cal

def load_calibration(json_path: str | Path) -> CDMSCalibration:
    """Load and return a CDMSCalibration from a JSON fit file."""
    return CDMSCalibration.load(json_path)

def process_frame(
    x: np.ndarray,
    E: float | List[float] | np.ndarray,
    V: Optional[float],
    *,
    calib: CDMSCalibration,
    reject_overlap_hz: Optional[float] = None,
) -> List[Ion]:
    """
    Run FFT + peak picking + m/z calculation on one frame.

    Requires prior configure_processing(fs_hz=...). If not configured, raises.
    """
    if _default_fft_cfg is None:
        raise RuntimeError("configure_processing(fs_hz=...) must be called before process_frame().")
    rtc = RealTimeCDMS(calib=calib, fft_cfg=_default_fft_cfg, charge_cal=_default_charge_cal)
    return rtc.process_frame(x, E_ev_per_z_for_peaks=E, V_volts=V, reject_overlap_hz=reject_overlap_hz)

# ------------------ Histogram helpers (optional) ------------------

from dataclasses import field

@dataclass
class HistState:
    field_name: str = "mz"          # "mz" or "m_amu"
    bins: int = 120
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    edges: Optional[np.ndarray] = None
    counts: Optional[np.ndarray] = None
    total_ions: int = 0

def update_hist(state: HistState, ions: List[Ion]) -> HistState:
    """Update histogram counts in-place for the given ions, returning the state."""
    if not ions:
        return state
    vals = np.array([
        getattr(i, state.field_name)
        for i in ions
        if getattr(i, state.field_name) is not None
    ], dtype=float)
    if vals.size == 0:
        return state
    vmin = float(np.min(vals)) if state.vmin is None else float(state.vmin)
    vmax = float(np.max(vals)) if state.vmax is None else float(state.vmax)
    if vmin == vmax:
        vmin *= 0.9; vmax *= 1.1
    # Initialize bins if needed
    if state.edges is None or state.counts is None:
        edges = np.linspace(vmin, vmax, int(state.bins) + 1)
        counts = np.zeros(int(state.bins), dtype=int)
        state.edges, state.counts = edges, counts
    # If values fall outside, expand range by rebinning (simple approach)
    if vals.min() < state.edges[0] or vals.max() > state.edges[-1]:
        vmin = min(vmin, float(state.edges[0])); vmax = max(vmax, float(state.edges[-1]))
        state.edges = np.linspace(vmin, vmax, int(state.bins) + 1)
        state.counts = np.zeros(int(state.bins), dtype=int)
    # Accumulate
    hist, _ = np.histogram(vals, bins=state.edges)
    state.counts += hist
    state.total_ions += int(vals.size)
    return state

def export_hist(state: HistState, path: str | Path) -> None:
    """Export histogram to CSV: start_edge,end_edge,count."""
    if state.edges is None or state.counts is None:
        Path(path).write_text("start,end,count\n", encoding="utf-8"); return
    with Path(path).open("w", encoding="utf-8") as f:
        f.write("start,end,count\n")
        for i in range(len(state.counts)):
            f.write(f"{state.edges[i]},{state.edges[i+1]},{int(state.counts[i])}\n")
