"""
hold_detector.py
----------------
Decide, chunk by chunk, whether a trapped particle is STILL passing the pickup
electrode, so the mirror can be held on until the particle is lost.

Pure numpy/scipy: no Qt, no hardware, no file I/O. It can be replayed offline
against saved PicoScope CSVs (see tests/pytest/test_hold_detector.py).

How it works
------------
The first sample fed in is the trigger instant (t = 0). Channel A is processed
as a stream:

  1. BLANKING    (0 .. blank_ms)  The mirror-switch transient. Data is ignored.
  2. CONFIRMING  A "pass" is one bipolar lobe pair on A. Passes are found as
                 peaks of the smoothed |A| envelope (so the + and - lobes of a
                 pass give ONE peak with steady timing). We need `min_passes`
                 passes with consistent spacing AND a periodic autocorrelation
                 (same helpers the catch classifier uses). If this has not
                 happened `confirm_deadline_ms` after blanking ends, the
                 trigger was false -> release with reason "no_catch".
  3. HOLDING     Predict when the next pass is due (tracked period), count a
                 "hit" when a pass lands within +/- tolerance of the prediction,
                 a "miss" when the window passes with nothing in it. A single
                 miss is normal; `max_missed` CONSECUTIVE misses means the
                 particle is gone -> release with reason "lost". Hold also
                 ends at `hold_max_s` ("max_hold").

Loss is decided by TIMING, not amplitude, so one-off noise bursts neither keep
the hold alive nor break it: a burst that does not fit the predicted timing is
simply not a hit.

The amplitude threshold for "is this envelope peak a pass" is a fraction
(`peak_frac`) of the 90th-percentile envelope level seen while confirming, then
frozen for the hold. (A white-noise sigma is not used: on the real traces the
noise floor is low-frequency wobble between passes, not sample noise.)

Latency: results for a sample are only final once the filters have seen about
`highpass_us/2 + envelope_us/2` of data after it, so a loss is reported roughly
`max_missed * period + that margin` after the last pass.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Deque, List, Optional, Tuple

import numpy as np
from scipy import signal
from scipy.ndimage import uniform_filter1d

from instrument_app.services.catch_classifier import (
    autocorr_unbiased,
    first_strong_peak,
)

# states
BLANKING = "BLANKING"
CONFIRMING = "CONFIRMING"
HOLDING = "HOLDING"
RELEASED = "RELEASED"

# release reasons
REASON_NO_CATCH = "no_catch"    # never confirmed before the deadline (false trigger)
REASON_LOST = "lost"            # max_missed consecutive predicted passes missing
REASON_MAX_HOLD = "max_hold"    # hard time cap


@dataclass
class HoldDetectorConfig:
    # --- timing (all measured from the trigger, i.e. the first sample fed) ---
    blank_ms: float = 3.5               # ignore A while the mirror switch rings
    confirm_deadline_ms: float = 10.0   # after blanking ends; no catch by then -> release
    hold_max_s: float = 3.0             # hard cap on the whole hold

    # --- pass detection ---
    highpass_us: float = 250.0          # moving-average length removed from A
    envelope_us: float = 100.0          # smoothing of |A|; wider than the lobe pair so a pass is one hump
    min_spacing_us: float = 100.0       # passes closer than this are one pass
    peak_frac: float = 0.40             # pass threshold = this x P90 of the envelope

    # --- confirmation ---
    min_passes: int = 5                 # passes with consistent spacing needed
    confirm_coherence: float = 0.40     # autocorrelation peak needed on A
    min_period_us: float = 50.0
    max_period_us: float = 2000.0

    # --- hold ---
    hold_frac: float = 0.40             # pass threshold = this x median height of recent hits
    height_history: int = 8             # hits used for that median
    tolerance: float = 0.20             # a pass is a hit within +/- this x period
    max_missed: int = 5                 # consecutive missed predictions -> lost
    period_history: int = 8             # gaps used for the median period
    miss_widen: float = 0.05            # hit window grows by this x period per consecutive miss
                                        # (prediction error accumulates while passes are missing)
    phase_gain: float = 0.5             # 0 = ignore where the pass landed, 1 = snap to it

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HoldStatus:
    state: str
    t_ms: float                         # time of the last sample fed (from trigger)
    period_us: Optional[float] = None
    hits: int = 0
    missed: int = 0                     # current consecutive misses
    total_missed: int = 0
    reason: str = ""                    # set once state == RELEASED
    released_at_ms: Optional[float] = None
    passes_seen: int = 0                # envelope peaks counted in the confirm train / hold

    def to_dict(self) -> dict:
        return asdict(self)


class HoldDetector:
    """Incremental detector. Call feed() with consecutive chunks of channel A
    (any units, consistent). Chunk size does not change the result, only how
    soon it is reported."""

    def __init__(self, sample_interval_ns: float,
                 config: Optional[HoldDetectorConfig] = None) -> None:
        if sample_interval_ns <= 0:
            raise ValueError("sample_interval_ns must be > 0")
        self.cfg = config or HoldDetectorConfig()
        self.dt_ns = float(sample_interval_ns)
        dt_us = self.dt_ns / 1000.0
        self._dt_us = dt_us

        c = self.cfg
        self._blank_n = int(round(c.blank_ms * 1000.0 / dt_us))
        self._deadline_n = int(round((c.blank_ms + c.confirm_deadline_ms) * 1000.0 / dt_us))
        self._hold_max_n = int(round(c.hold_max_s * 1e6 / dt_us))
        self._hp_n = max(3, int(round(c.highpass_us / dt_us)))
        self._env_n = max(3, int(round(c.envelope_us / dt_us)))
        self._min_sp_n = max(1, int(round(c.min_spacing_us / dt_us)))
        self._margin = self._hp_n // 2 + self._env_n // 2 + 2
        self.reset()

    # ------------------------------------------------------------------

    def reset(self) -> None:
        self._n = 0                         # total samples fed
        self._buf = np.zeros(0)             # post-blank samples (gaps filled)
        self._buf0 = self._blank_n          # absolute index of _buf[0]
        self._last_good = 0.0
        self._state = BLANKING
        self._reason = ""
        self._released_at_n: Optional[int] = None

        self._decided: Optional[int] = None   # absolute idx: peaks below it are final
        self._stat_upto = 0
        self._last_peak_hold: Optional[int] = None
        self._env_stats = np.zeros(0)         # finalized envelope values (confirming)
        self._cands: List[Tuple[int, float]] = []   # (abs idx, envelope height)
        self._thr = 0.0

        self._period_n: Optional[float] = None
        self._gaps: Deque[float] = deque(maxlen=self.cfg.period_history)
        self._last_hit: Optional[int] = None
        self._next_expected: float = 0.0
        self._pending: List[Tuple[int, float]] = []   # (abs idx, height) not yet judged
        self._hit_heights: Deque[float] = deque(maxlen=self.cfg.height_history)
        self._hits = 0
        self._missed = 0
        self._total_missed = 0
        self._train_len = 0

    # ------------------------------------------------------------------
    # public

    @property
    def released(self) -> bool:
        return self._state == RELEASED

    def status(self) -> HoldStatus:
        period_us = None if self._period_n is None else self._period_n * self._dt_us
        rel_ms = None if self._released_at_n is None else self._released_at_n * self._dt_us / 1000.0
        return HoldStatus(
            state=self._state,
            t_ms=self._n * self._dt_us / 1000.0,
            period_us=period_us,
            hits=self._hits,
            missed=self._missed,
            total_missed=self._total_missed,
            reason=self._reason,
            released_at_ms=rel_ms,
            passes_seen=self._train_len,
        )

    def feed(self, chunk) -> HoldStatus:
        """Add the next chunk of channel A and return the current status.
        After release further data is ignored."""
        if self._state == RELEASED:
            return self.status()
        x = self._clean(np.asarray(chunk, dtype=np.float64).ravel())
        if x.size == 0:
            return self.status()

        start = self._n
        self._n += x.size
        # drop the blanking part
        if self._n <= self._blank_n:
            return self.status()
        if start < self._blank_n:
            x = x[self._blank_n - start:]
        if self._state == BLANKING:
            self._state = CONFIRMING
        self._buf = np.concatenate([self._buf, x]) if self._buf.size else x.copy()

        self._process()

        # time-based endings (checked on the fed time, not the finalized time)
        if self._state == CONFIRMING and self._n >= self._deadline_n:
            self._release(REASON_NO_CATCH)
        elif self._state == HOLDING and self._n >= self._hold_max_n:
            self._release(REASON_MAX_HOLD)
        return self.status()

    # ------------------------------------------------------------------
    # internals

    def _clean(self, x: np.ndarray) -> np.ndarray:
        """Replace NaN/inf (overrange markers) by interpolation, carrying the
        last good value across chunk boundaries."""
        bad = ~np.isfinite(x)
        if bad.any():
            good = np.flatnonzero(~bad)
            if good.size == 0:
                x = np.full_like(x, self._last_good)
            else:
                x = x.copy()
                x[bad] = np.interp(np.flatnonzero(bad), good, x[good])
        if x.size:
            self._last_good = float(x[-1])
        return x

    def _release(self, reason: str) -> None:
        self._state = RELEASED
        self._reason = reason
        self._released_at_n = self._n
        self._buf = np.zeros(0)
        self._env_stats = np.zeros(0)

    def _process(self) -> None:
        m = self._margin
        n_buf = self._buf.size
        if n_buf <= 2 * m + 3:
            return
        x = self._buf
        hp = x - uniform_filter1d(x, size=self._hp_n, mode="reflect")
        env = uniform_filter1d(np.abs(hp), size=self._env_n, mode="reflect")

        if self._decided is None:
            # skip the filter start-up region of the very first samples
            self._decided = self._buf0 + m
            self._stat_upto = self._decided

        hi = n_buf - m                                   # relative, exclusive
        # a peak is final only once every sample within min_spacing after it is
        # final too (otherwise a taller neighbour could still appear)
        limit = self._buf0 + hi - self._min_sp_n          # absolute, exclusive
        a = max(0, self._decided - self._buf0 - self._min_sp_n - 1)
        if hi - a < 3:
            return

        if self._state == CONFIRMING:
            s0 = self._stat_upto - self._buf0
            if hi > s0:
                self._env_stats = np.concatenate([self._env_stats, env[s0:hi]])
                self._stat_upto = self._buf0 + hi

        # local maxima of the envelope, separated by min_spacing (taller wins)
        seg = env[a:hi]
        pk, _ = signal.find_peaks(seg, distance=self._min_sp_n)
        for p in pk:
            idx = self._buf0 + a + int(p)
            if idx < self._decided or idx >= limit:
                continue
            if self._cands_last() is not None and idx - self._cands_last() < self._min_sp_n:
                continue
            self._add_peak(idx, float(seg[p]))
        self._decided = max(self._decided, limit)

        if self._state == CONFIRMING:
            self._try_confirm(hp)
        if self._state == HOLDING:
            self._advance_hold()
            self._trim()

    def _cands_last(self) -> Optional[int]:
        if self._state == CONFIRMING:
            return self._cands[-1][0] if self._cands else None
        if self._pending:
            return self._pending[-1][0]
        return self._last_peak_hold

    def _add_peak(self, idx: int, height: float) -> None:
        if self._state == CONFIRMING:
            self._cands.append((idx, height))
        else:
            self._pending.append((idx, height))
            self._last_peak_hold = idx

    def _threshold(self) -> float:
        if self._env_stats.size == 0:
            return float("inf")
        return self.cfg.peak_frac * float(np.percentile(self._env_stats, 90))

    def _try_confirm(self, hp: np.ndarray) -> None:
        c = self.cfg
        thr = self._threshold()
        idxs = [i for i, h in self._cands if h >= thr]
        if len(idxs) < c.min_passes:
            return
        # longest recent run of passes whose gaps agree with their median
        train = None
        for m in range(len(idxs), c.min_passes - 1, -1):
            sub = idxs[-m:]
            gaps = np.diff(sub)
            med = float(np.median(gaps))
            if med > 0 and np.all(np.abs(gaps - med) <= c.tolerance * med):
                train = sub
                break
        if train is None:
            return
        self._train_len = len(train)
        med_gap = float(np.median(np.diff(train)))
        period_us = med_gap * self._dt_us
        if not (c.min_period_us <= period_us <= c.max_period_us):
            return

        # periodicity check on the high-passed A seen since blanking
        seg = hp[self._margin: self._buf.size - self._margin]
        max_lag = min(int(c.max_period_us / self._dt_us), seg.size // 3)
        min_lag = max(2, int(c.min_period_us / self._dt_us))
        if max_lag <= min_lag + 2:
            return
        r = autocorr_unbiased(seg, max_lag)
        lag = first_strong_peak(r, min_lag, max_lag)
        if lag is None or r[lag] < c.confirm_coherence:
            return
        if abs(lag - med_gap) > c.tolerance * med_gap:
            return

        # confirmed: from here the threshold follows the passes' own height
        self._period_n = med_gap
        self._gaps.extend(np.diff(train).tolist())
        self._last_hit = train[-1]
        # anchor on the median phase of the whole train, not on its last peak
        # (a single peak can sit tens of us off the true pass centre)
        tr = np.asarray(train, dtype=np.float64)
        phase = float(np.median(tr - np.arange(tr.size) * med_gap))
        self._next_expected = phase + tr.size * med_gap
        self._hits = len(train)
        self._missed = 0
        self._hit_heights.extend(h for i, h in self._cands if i in set(train) and h >= thr)
        self._thr = self.cfg.hold_frac * float(np.median(self._hit_heights))
        self._pending = [(i, h) for i, h in self._cands if i > train[-1]]
        self._last_peak_hold = self._cands[-1][0] if self._cands else None
        self._cands = []
        self._env_stats = np.zeros(0)
        self._state = HOLDING

    def _advance_hold(self) -> None:
        c = self.cfg
        decided = self._decided
        while True:
            P = self._period_n
            T = self._next_expected
            half = min(0.5, c.tolerance + c.miss_widen * self._missed) * P
            lo, hi = T - half, T + half
            if decided <= hi:
                break                       # window not fully decided yet
            self._pending = [q for q in self._pending if q[0] >= lo]
            cand = [q for q in self._pending if q[0] <= hi and q[1] >= self._thr]
            if cand:
                p, h = min(cand, key=lambda q: abs(q[0] - T))
                self._hit_heights.append(h)
                self._thr = self.cfg.hold_frac * float(np.median(self._hit_heights))
                k = max(1, int(round((p - self._last_hit) / P)))
                self._gaps.append((p - self._last_hit) / k)
                self._period_n = float(np.median(self._gaps))
                self._last_hit = p
                # nudge (not snap) the prediction toward the observed pass
                self._next_expected = T + self._period_n + c.phase_gain * (p - T)
                self._hits += 1
                self._missed = 0
                self._pending = [q for q in self._pending if q[0] > p]
            else:
                self._missed += 1
                self._total_missed += 1
                self._next_expected = T + P
                if self._missed >= c.max_missed:
                    self._release(REASON_LOST)
                    return

    def _trim(self) -> None:
        """Drop samples no longer needed (bounded memory during long holds)."""
        keep_from = self._decided - self._min_sp_n - self._margin - 2
        drop = keep_from - self._buf0
        if drop > 0:
            self._buf = self._buf[drop:]
            self._buf0 += drop
