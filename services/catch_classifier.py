"""
catch_classifier.py
--------------------
Decide whether one triggered A+B waveform contains a *caught particle*.

Pure numpy/scipy: no Qt, no hardware, no file I/O, so it can be unit-tested
offline and replayed against saved PicoScope CSVs.

What a catch looks like (from the first captured example)
---------------------------------------------------------
  * A mirror-switching transient shortly after the trigger (a few ms in) that
    overranges both channels. This is NOT part of the particle signal.
  * After that transient settles, the trapped particle oscillates through the
    pickup electrode and produces a *repeating* pattern:
      - Channel A (filtered):   repeating bipolar lobes (positive + negative).
      - Channel B (less filtered): a roughly square-wave pattern at the SAME
        repetition period, riding on noise and a slow recovery of the baseline.

The oscillation frequency encodes the particle's kinetic energy, so there is
deliberately NO fixed frequency window. The only limits on the period are
geometric: the pattern must repeat at least `min_cycles` times inside the
analysis window, and be slower than `min_period_us` (faster than the noise
correlation time). Both are configurable.

How the decision is made
------------------------
Everything is judged on the analysis window only (after the mirror transient).
Each channel is high-passed to remove the slow baseline recovery, then:

  1. A repeats:  normalized autocorrelation of A has a peak (>= a_coherence_min)
     at some period within the allowed range.
  2. B repeats at the same period (>= b_coherence_min at A's period +/- tol).
  3. Both channels have a repeating signal well above their own noise: the trace
     is folded at the found period and the folded ("template") waveform is
     compared with what is left over cycle to cycle. Folding measures noise
     correctly even when the noise is strongly filtered/colored.
  4. A is bipolar: the folded A waveform has a significant positive AND negative
     lobe of comparable size.
  5. B resembles a square wave: its folded waveform has harmonic content (pure
     sinusoidal pickup does not).

All gates must pass. Every feature is returned so thresholds can be tuned from
real saved waveforms, and `reason` names the first gate that failed.

The thresholds are starting points chosen from ONE captured example; they
should be re-tuned once non-catch waveforms are available.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np
from scipy import signal
from scipy.ndimage import uniform_filter1d


# ---------------------------------------------------------------------------
# Configuration / result
# ---------------------------------------------------------------------------

@dataclass
class CatchClassifierConfig:
    # --- analysis window (measured from the first sample of the trace) ---
    # Skip the trigger transient and the mirror-switching transient plus its
    # recovery tail. In the first capture the mirror switch is at ~2.2 ms and
    # the overshoot has settled by ~3.3 ms.
    analysis_start_ms: float = 3.5
    analysis_end_ms: Optional[float] = None     # None = end of trace

    # --- repetition search range (no physics limit on the particle) ---
    min_cycles: float = 5.0         # pattern must repeat at least this often
    min_period_us: float = 20.0     # shorter than this is noise, not oscillation

    # --- gates ---
    a_coherence_min: float = 0.40   # autocorrelation peak of A
    b_coherence_min: float = 0.30   # autocorrelation of B at A's period
    period_tolerance: float = 0.15  # fractional window around A's period for B
    fold_snr_min: float = 1.0       # folded-waveform rms / cycle-to-cycle residual rms
    lobe_significance: float = 5.0  # lobe height must exceed this x the template's
                                    # standard error (residual rms / sqrt(cycles))
    lobe_balance_min: float = 0.30  # smaller / larger of the +/- lobe heights in A

    # B should resemble a square wave: its folded waveform must carry harmonic
    # content. The first capture measures 0.71; an ideal square is ~0.39 and a
    # pure sine ~0, so 0.10 only rejects clearly sinusoidal pickup. 0 disables.
    # (Missing a real catch costs far more than keeping an extra false one, so
    # this is kept lenient until non-catch examples exist.)
    b_shape_ratio_min: float = 0.10

    # max fraction of non-finite (overrange) samples tolerated in the window
    max_nonfinite_fraction: float = 0.05

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CatchResult:
    is_catch: bool
    reason: str                         # "ok" or the first failed gate

    # period of the repeating pattern found on A
    period_us: Optional[float] = None
    frequency_hz: Optional[float] = None

    # features (always filled when computable; useful for tuning)
    a_coherence: float = 0.0
    b_coherence: float = 0.0
    a_snr: float = 0.0                  # folded SNR (see fold_snr_min)
    b_snr: float = 0.0
    n_pos_lobes: int = 0                # lobes per period in the folded A waveform
    n_neg_lobes: int = 0
    lobe_balance: float = 0.0
    n_cycles: float = 0.0
    nonfinite_fraction: float = 0.0
    b_shape_ratio: Optional[float] = None        # B harmonics 2-5 / fundamental
                                                 # (square wave ~0.39, sine ~0)
    ab_derivative_corr: Optional[float] = None   # |corr(A, dB/dt)|, +/-20 us lag;
                                                 # recorded only, not a gate (a pure
                                                 # sine pickup scores high too)

    def to_dict(self) -> dict:
        return asdict(self)


def _reject(reason: str, **features) -> CatchResult:
    return CatchResult(is_catch=False, reason=reason, **features)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fill_nonfinite(x: np.ndarray) -> np.ndarray:
    """Linearly interpolate across NaN/inf samples (overrange markers)."""
    bad = ~np.isfinite(x)
    if not bad.any():
        return x
    good_idx = np.flatnonzero(~bad)
    if good_idx.size < 2:
        return np.zeros_like(x)
    out = x.copy()
    out[bad] = np.interp(np.flatnonzero(bad), good_idx, x[good_idx])
    return out


def _highpass(x: np.ndarray, length: int) -> np.ndarray:
    """Remove slow structure by subtracting a moving average of `length` samples."""
    length = max(3, int(length))
    return x - uniform_filter1d(x, size=length, mode="reflect")


def _fold(x: np.ndarray, lag: int):
    """Fold x at `lag` samples. Returns (template, residual_rms, n_cycles):
    the cycle-averaged waveform, the rms of what is left after subtracting it
    from every cycle, and the number of whole cycles used."""
    n_cyc = x.size // lag
    if n_cyc < 2:
        return np.zeros(lag), float("inf"), n_cyc
    rows = x[: n_cyc * lag].reshape(n_cyc, lag)
    template = rows.mean(axis=0)
    resid = float((rows - template).std())
    return template, resid, n_cyc


def _autocorr_unbiased(x: np.ndarray, max_lag: int) -> np.ndarray:
    """Normalized, overlap-corrected autocorrelation r[0..max_lag] via FFT.
    r[0] == 1. Overlap correction stops long lags being penalized."""
    n = x.size
    x = x - x.mean()
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    spec = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(spec * np.conj(spec), nfft)[: max_lag + 1]
    ac = ac / (n - np.arange(max_lag + 1))      # per-lag mean product
    return ac / ac[0] if ac[0] > 0 else np.zeros(max_lag + 1)


def _first_strong_peak(r: np.ndarray, min_lag: int, max_lag: int,
                       strength: float = 0.85) -> Optional[int]:
    """Smallest-lag local maximum of r within [min_lag, max_lag] whose height is
    at least `strength` x the best peak. Prefers the fundamental period over its
    multiples (which have near-equal height)."""
    if max_lag <= min_lag + 2:
        return None
    seg = r[min_lag: max_lag + 1]
    peaks, _ = signal.find_peaks(seg)
    if peaks.size == 0:
        return None
    heights = seg[peaks]
    best = heights.max()
    for p, h in zip(peaks, heights):
        if h >= strength * best:
            return int(p + min_lag)
    return None


def trigger_was_met(a_v: np.ndarray, trigger_enabled: bool, threshold_v: float,
                    direction: str, first_n: int = 10) -> bool:
    """True if channel A actually crossed the trigger threshold at the start of the
    trace. A trace that arrives because the scope's auto-trigger timer expired
    (no real signal) does not, and must not be counted as a trigger.

    With trigger disabled (free-run) every trace counts."""
    if not trigger_enabled:
        return True
    head = a_v[:first_n]
    head = head[np.isfinite(head)]
    if head.size == 0:
        return True     # cannot tell (overrange at the start); do not discard
    d = direction.upper()
    if d in ("RISING", "ABOVE"):
        return bool(head.max() >= threshold_v)
    if d in ("FALLING", "BELOW"):
        return bool(head.min() <= threshold_v)
    return bool(head.max() >= threshold_v or head.min() <= threshold_v)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def classify_catch(
    a: np.ndarray,
    b: np.ndarray,
    sample_interval_ns: float,
    config: Optional[CatchClassifierConfig] = None,
) -> CatchResult:
    """Classify one A+B trace pair. Units are arbitrary but must be consistent
    within each channel (all gates are relative to that channel's own noise)."""
    cfg = config or CatchClassifierConfig()
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape or a.ndim != 1:
        raise ValueError("A and B must be 1-D arrays of equal length")
    dt_us = sample_interval_ns / 1000.0

    # ---- analysis window ----
    i0 = max(0, int(round(cfg.analysis_start_ms * 1000.0 / dt_us)))
    i1 = a.size if cfg.analysis_end_ms is None else min(
        a.size, int(round(cfg.analysis_end_ms * 1000.0 / dt_us)))
    n = i1 - i0
    window_us = n * dt_us
    max_period_us = window_us / cfg.min_cycles
    min_lag = max(2, int(np.ceil(cfg.min_period_us / dt_us)))
    max_lag = int(max_period_us / dt_us)
    if n < 8 * min_lag or max_lag <= min_lag + 2:
        return _reject("analysis window too short for the repetition search")

    wa, wb = a[i0:i1], b[i0:i1]
    nonfinite = float((~np.isfinite(wa)).mean())
    nonfinite = max(nonfinite, float((~np.isfinite(wb)).mean()))
    if nonfinite > cfg.max_nonfinite_fraction:
        return _reject(
            f"overrange in analysis window ({nonfinite:.0%} of samples)",
            nonfinite_fraction=nonfinite,
        )
    wa, wb = _fill_nonfinite(wa), _fill_nonfinite(wb)

    # ---- high-pass: remove slow baseline recovery ----
    # First pass uses a long average (period still unknown); the second pass,
    # once the period is known, averages over an exact number of periods so the
    # repeating signal is not touched while the slow recovery curve is removed.
    ha = _highpass(wa, max_lag)
    feats = dict(nonfinite_fraction=nonfinite)

    # ---- gate 1: A repeats ----
    ra = _autocorr_unbiased(ha, max_lag)
    lag = _first_strong_peak(ra, min_lag, max_lag)
    if lag is None:
        return _reject("no repeating pattern on A", **feats)
    a_coh = float(ra[lag])
    period_us = lag * dt_us
    feats.update(
        a_coherence=a_coh, period_us=period_us, frequency_hz=1e6 / period_us,
        n_cycles=window_us / period_us,
    )
    if a_coh < cfg.a_coherence_min:
        return _reject(
            f"A repetition too weak ({a_coh:.2f} < {cfg.a_coherence_min:.2f})", **feats)

    ha = _highpass(wa, 2 * lag)
    hb = _highpass(wb, 2 * lag)

    # ---- gate 2: B repeats at the same period ----
    rb = _autocorr_unbiased(hb, max_lag)
    lo = max(min_lag, int(lag * (1 - cfg.period_tolerance)))
    hi = min(max_lag, int(np.ceil(lag * (1 + cfg.period_tolerance))))
    b_coh = float(rb[lo: hi + 1].max()) if hi >= lo else 0.0
    feats["b_coherence"] = b_coh
    if b_coh < cfg.b_coherence_min:
        return _reject(
            f"B does not repeat at A's period ({b_coh:.2f} < {cfg.b_coherence_min:.2f})",
            **feats)

    # ---- gate 3: repeating signal well above noise in both channels ----
    tmpl_a, res_a, n_cyc = _fold(ha, lag)
    tmpl_b, res_b, _ = _fold(hb, lag)
    a_snr = float(tmpl_a.std() / res_a) if res_a > 0 else float("inf")
    b_snr = float(tmpl_b.std() / res_b) if res_b > 0 else float("inf")
    feats.update(a_snr=a_snr, b_snr=b_snr)
    if a_snr < cfg.fold_snr_min:
        return _reject(f"A repeating signal too close to noise (SNR {a_snr:.2f})", **feats)
    if b_snr < cfg.fold_snr_min:
        return _reject(f"B repeating signal too close to noise (SNR {b_snr:.2f})", **feats)

    # B square-wave-ness: energy in harmonics 2-5 relative to the fundamental
    # of the folded B waveform (ideal square ~0.39, pure sine ~0)
    spec_b = np.abs(np.fft.rfft(tmpl_b - tmpl_b.mean()))
    if spec_b.size > 5 and spec_b[1] > 0:
        feats["b_shape_ratio"] = float(np.sqrt((spec_b[2:6] ** 2).sum()) / spec_b[1])

    # ---- gate 4: A is bipolar ----
    tmpl = tmpl_a - tmpl_a.mean()
    stderr = res_a / np.sqrt(n_cyc)         # noise on each point of the template
    pos_h, neg_h = float(tmpl.max()), float(-tmpl.min())
    feats["lobe_balance"] = min(pos_h, neg_h) / max(pos_h, neg_h) if max(pos_h, neg_h) > 0 else 0.0
    ptp = pos_h + neg_h
    # count lobes per period on a circularly padded template so a lobe that
    # straddles the fold point is not split in two
    pad = np.concatenate([tmpl, tmpl, tmpl])
    prom = max(cfg.lobe_significance * stderr, 0.1 * ptp)
    pk_pos, _ = signal.find_peaks(pad, prominence=prom)
    pk_neg, _ = signal.find_peaks(-pad, prominence=prom)
    feats["n_pos_lobes"] = int(((pk_pos >= lag) & (pk_pos < 2 * lag)).sum())
    feats["n_neg_lobes"] = int(((pk_neg >= lag) & (pk_neg < 2 * lag)).sum())
    sig_h = cfg.lobe_significance * stderr
    if pos_h < sig_h or neg_h < sig_h or feats["n_pos_lobes"] < 1 or feats["n_neg_lobes"] < 1:
        return _reject(
            f"A not bipolar ({feats['n_pos_lobes']} positive / {feats['n_neg_lobes']} negative lobes)",
            **feats)
    if feats["lobe_balance"] < cfg.lobe_balance_min:
        return _reject(f"A lobes lopsided (balance {feats['lobe_balance']:.2f})", **feats)

    # ---- A should look like d(B)/dt ----
    try:
        db = np.gradient(uniform_filter1d(hb, size=5, mode="nearest"))
        da = (ha - ha.mean()) / (ha.std() or 1.0)
        dbn = (db - db.mean()) / (db.std() or 1.0)
        maxshift = max(1, int(round(20.0 / dt_us)))
        best = max(
            abs(float(np.mean(da[max(0, s):n + min(0, s)] * dbn[max(0, -s):n - max(0, s)])))
            for s in range(-maxshift, maxshift + 1)
        )
        feats["ab_derivative_corr"] = best
    except (ValueError, FloatingPointError):
        pass

    # ---- gate 5: B resembles a square wave (disabled when threshold is 0) ----
    shape = feats.get("b_shape_ratio")
    if cfg.b_shape_ratio_min > 0 and (shape is None or shape < cfg.b_shape_ratio_min):
        return _reject(
            f"B not square-like (harmonic ratio {shape if shape is not None else 0:.2f} "
            f"< {cfg.b_shape_ratio_min:.2f})", **feats)

    return CatchResult(is_catch=True, reason="ok", **feats)
