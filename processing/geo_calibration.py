
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
try:
    from scipy.signal import get_window, find_peaks  # type: ignore
    _HAVE_SCIPY = True
except Exception:  # pragma: no cover - optional dependency at runtime
    _HAVE_SCIPY = False
    def get_window(*_, **__):  # type: ignore
        raise ImportError("SciPy is required for signal processing. Install with: pip install scipy")
    def find_peaks(*_, **__):  # type: ignore
        raise ImportError("SciPy is required for peak picking. Install with: pip install scipy")

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
    r2: Optional[float] = None
    metadata: Optional[Dict[str, float]] = None

    @classmethod
    def load(cls, path: str | Path) -> "CDMSCalibration":
        js = json.loads(Path(path).read_text(encoding="utf-8"))
        # Allow combined summary (from --summary)
        if "files" in js and isinstance(js["files"], list) and js["files"]:
            js = js["files"][0]
        poly = js.get("C_poly", None)
        if not poly:
            raise ValueError("JSON has no C_poly. Run cdms_geometry_fit_v3.py with eV_per_z present.")
        return cls(CPoly.from_json(poly), js.get("voltage_median", None), js.get("r2", None), js.get("metadata", None))

    def coefficients(self) -> Dict[str, float]:
        return {
            "A00": self.cpoly.A00, "A10": self.cpoly.A10, "A01": self.cpoly.A01, "A20": self.cpoly.A20,
            "A11": self.cpoly.A11, "A02": self.cpoly.A02, "A30": self.cpoly.A30, "A21": self.cpoly.A21,
            "A12": self.cpoly.A12, "A03": self.cpoly.A03,
        }

    def summary(self) -> Dict[str, float | None]:  # type: ignore[valid-type]
        out: Dict[str, float | None] = {**self.coefficients()}
        out["voltage_median"] = self.voltage_median
        out["r2"] = self.r2
        return out

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
    if not _HAVE_SCIPY:
        raise ImportError("SciPy is required to process frames. Please install it: pip install scipy")
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

def build_charge_cal_from_calibration(calib: CDMSCalibration) -> Optional[ChargeCal]:
    """Construct a simple charge calibration from calibration metadata if available.

    If calibration JSON contains keys: charge_k, charge_f0, charge_alpha,
    use q = charge_k * amp * (f/charge_f0)^charge_alpha. Otherwise returns None.
    """
    md = getattr(calib, 'metadata', None) or {}
    if 'charge_k' in md and 'charge_f0' in md:
        k = float(md['charge_k']); f0 = float(md['charge_f0']); alpha = float(md.get('charge_alpha', 0.0))
        def _cal(amp: float, f_hz: float) -> float:
            return float(k * amp * ((f_hz / f0) ** alpha if f_hz > 0 else 1.0))
        return _cal
    return None

# ------------------ Main per-frame processor ------------------

@dataclass
class IonResult:
    f_hz: float
    amp: float
    E_ev_per_z: Optional[float]
    V_volts: float
    mz: Optional[float]
    z: Optional[float] = None
    m_amu: Optional[float] = None
    quality: str = "ok"
    snr_db: Optional[float] = None
    fwhm_hz: Optional[float] = None
    r2_over_r1: Optional[float] = None
    r3_over_r1: Optional[float] = None

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

        # Compute FFT spectrum for resolution and harmonic ratios
        N = len(x)
        nfft = self.fft_cfg.nfft or int(2**math.ceil(math.log2(N)))
        w = get_window(self.fft_cfg.window, N, fftbins=True)
        X = np.fft.rfft(x * w, n=nfft)
        freqs = np.fft.rfftfreq(nfft, d=1.0/self.fft_cfg.fs)
        mag = np.abs(X)
        df = freqs[1] - freqs[0] if len(freqs) > 1 else self.fft_cfg.fs / nfft

        # Effective frequency resolution: ~1 bin by default, allow override
        res_hz = max(df, float(reject_overlap_hz) if reject_overlap_hz else df)

        # Baseline E array from argument
        if np.isscalar(E_ev_per_z_for_peaks):
            E_base = np.full(len(peaks), float(E_ev_per_z_for_peaks), dtype=float)
        else:
            E_base = np.asarray(E_ev_per_z_for_peaks, dtype=float)
            if E_base.shape[0] != len(peaks):
                raise ValueError("Length of E_ev_per_z_for_peaks must match number of detected peaks.")

        # Overlap detection among sorted peaks
        idx_sorted = sorted(range(len(peaks)), key=lambda i: peaks[i].f_hz)
        overlap = [False] * len(peaks)
        for a, b in zip(idx_sorted[:-1], idx_sorted[1:]):
            if abs(peaks[b].f_hz - peaks[a].f_hz) < res_hz:
                overlap[a] = True; overlap[b] = True

        # Rough noise estimate for SNR
        start = int(0.6 * len(mag))
        noise_rms = float(np.std(mag[start:])) if start < len(mag) else float(np.std(mag))

        # Per-peak harmonic-based E estimate helper
        md = getattr(self.calib, 'metadata', None) or {}
        have_harm_model = ('harm_a0' in md) and (('harm_a2' in md) or ('harm_a3' in md))

        def near_amp(f):
            if f <= 0:
                return None
            k = int(round(f / df))
            if 0 <= k < len(mag):
                lo = max(0, k-1); hi = min(len(mag)-1, k+1)
                return float(np.max(mag[lo:hi+1]))
            return None

        results: List[IonResult] = []
        for i, p in enumerate(peaks):
            a1 = near_amp(p.f_hz)
            a2 = near_amp(2*p.f_hz)
            a3 = near_amp(3*p.f_hz)
            r2 = (a2 / a1) if (a1 and a1 > 0 and a2 is not None) else None
            r3 = (a3 / a1) if (a1 and a1 > 0 and a3 is not None) else None

            e_est: Optional[float] = None
            if have_harm_model:
                e_est = float(md.get('harm_a0', 0.0))
                if r2 is not None and 'harm_a2' in md:
                    e_est += float(md['harm_a2']) * float(r2)
                if r3 is not None and 'harm_a3' in md:
                    e_est += float(md['harm_a3']) * float(r3)

            E_val = float(E_base[i]) if e_est is None else float(e_est)

            # Quality and metrics
            quality = "ok" if not overlap[i] else "overlap"
            snr = float(p.amp / (noise_rms + 1e-12))
            snr_db = 20.0 * math.log10(max(snr, 1e-9))

            # Compute m/z only if usable
            mz_val: Optional[float] = None
            z_val: Optional[float] = None
            m_val: Optional[float] = None
            if quality == "ok":
                Cval = float(self.calib.cpoly.C(np.array([E_val], dtype=float), V)[0])
                mz_val = Cval / (p.f_hz ** 2)
                if self.charge_cal is not None:
                    q_coul = self.charge_cal(p.amp, p.f_hz)
                    z_val = q_coul / E_CHARGE
                    m_val = z_val * mz_val

            results.append(IonResult(
                f_hz=p.f_hz, amp=p.amp, E_ev_per_z=E_val, V_volts=V,
                mz=mz_val, z=z_val, m_amu=m_val, quality=quality,
                snr_db=snr_db, fwhm_hz=None, r2_over_r1=r2, r3_over_r1=r3,
            ))
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

def process_frame_with_event(
    x: np.ndarray,
    E: float | List[float] | np.ndarray,
    V: Optional[float],
    *,
    calib: CDMSCalibration,
    reject_overlap_hz: Optional[float] = None,
) -> Tuple[List[Ion], Dict[str, object]]:
    """
    Run processing and also return an event classification summary dict.

    Summary keys: cls('empty'|'single'|'multi'|'ambiguous'), n_peaks, n_usable.
    """
    if _default_fft_cfg is None:
        raise RuntimeError("configure_processing(fs_hz=...) must be called before process_frame().")
    rtc = RealTimeCDMS(calib=calib, fft_cfg=_default_fft_cfg, charge_cal=_default_charge_cal)
    ions = rtc.process_frame(x, E_ev_per_z_for_peaks=E, V_volts=V, reject_overlap_hz=reject_overlap_hz)
    n_peaks = len(ions)
    n_usable = sum(1 for i in ions if i.quality == "ok" and i.mz is not None)
    if n_peaks == 0:
        ev = {"cls": "empty", "n_peaks": 0, "n_usable": 0}
    else:
        amb = any(i.quality != "ok" for i in ions)
        if n_usable == 0:
            ev = {"cls": "ambiguous", "n_peaks": n_peaks, "n_usable": 0}
        elif n_usable == 1 and not amb:
            ev = {"cls": "single", "n_peaks": n_peaks, "n_usable": 1}
        elif not amb:
            ev = {"cls": "multi", "n_peaks": n_peaks, "n_usable": n_usable}
        else:
            ev = {"cls": "ambiguous", "n_peaks": n_peaks, "n_usable": n_usable}
    return ions, ev

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

# ------------------ Calibration verification helpers ------------------

def verify_calibration(
    ions: List[Ion],
    expected_mz: float,
    *,
    use_quality_ok_only: bool = True,
) -> Dict[str, float]:
    """Compute mass bias and precision for a calibrant with known m/z."""
    if use_quality_ok_only:
        vals = np.array([i.mz for i in ions if i.mz is not None and i.quality == "ok"], dtype=float)
    else:
        vals = np.array([i.mz for i in ions if i.mz is not None], dtype=float)
    if vals.size == 0:
        return {"n": 0, "mean_mz": float("nan"), "bias_ppm": float("nan"), "stdev_ppm": float("nan")}
    mean_mz = float(np.mean(vals))
    bias_ppm = float((mean_mz - expected_mz) / expected_mz * 1e6)
    stdev_ppm = float(np.std(vals) / expected_mz * 1e6)
    return {"n": int(vals.size), "mean_mz": mean_mz, "bias_ppm": bias_ppm, "stdev_ppm": stdev_ppm}
