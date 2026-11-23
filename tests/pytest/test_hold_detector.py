"""
test_hold_detector.py
---------------------
Offline tests for services/hold_detector.py. No Qt, no hardware.

Real data: tests/pytest/data/catch_example_FIGURE_3.csv.gz (a PicoScope export,
5 MS/s, 50 ms, one captured particle that never leaves). The detector must
confirm it and never release it, despite single dropped passes and a large noise
burst at ~11.8 ms.

Synthetic cases build pass trains with the measured character (bipolar lobe
pair every ~231 us, low-frequency wobble) so loss / false-trigger / drift
behaviour can be checked, since the real file contains no loss.

Run with:
    python -m pytest tests/pytest/test_hold_detector.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import numpy as np
import pytest
from scipy.signal import lfilter

from instrument_app.services.catch_recorder import load_pico_csv
from instrument_app.services.hold_detector import (
    BLANKING,
    CONFIRMING,
    HOLDING,
    RELEASED,
    HoldDetector,
    HoldDetectorConfig,
)

FIGURE_3 = Path(__file__).parent / "data" / "catch_example_FIGURE_3.csv.gz"
DT_NS = 200.0
DT_US = DT_NS / 1000.0
FS_PER_MS = int(1000 / DT_US)           # samples per ms


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def run(a, chunk_ms=0.5, cfg=None, dt_ns=DT_NS, stop_on_release=True):
    """Feed `a` in chunks. Returns (detector, list of statuses)."""
    det = HoldDetector(dt_ns, cfg)
    n = max(1, int(chunk_ms * 1000 / (dt_ns / 1000)))
    out = []
    for i in range(0, len(a), n):
        st = det.feed(a[i:i + n])
        out.append(st)
        if stop_on_release and st.state == RELEASED:
            break
    return det, out


def _colored(rng, n, sigma, rho=0.982):
    x = lfilter([1.0], [1.0, -rho], rng.standard_normal(n + 2000))[2000:]
    return x / x.std() * sigma


def _lobe_pair(amp=9.0, sep_us=60.0, sigma_us=12.0):
    """One pass: positive lobe then a slightly deeper negative lobe."""
    t = np.arange(-6 * sigma_us, sep_us + 6 * sigma_us, DT_US)
    return amp * (np.exp(-0.5 * (t / sigma_us) ** 2)
                  - 1.1 * np.exp(-0.5 * ((t - sep_us) / sigma_us) ** 2))


def make_train(rng, total_ms=50.0, period_us=231.0, drift=0.0, jitter_us=6.0,
               start_ms=3.6, end_ms=None, drop=(), amp=9.0, noise_mv=1.0,
               amp_var=0.15):
    """Pass train on coloured noise, with a large 'switch transient' before 3 ms.

    drift   fractional period change over the whole record (0.04 = +4 %)
    end_ms  particle leaves here (no passes after)
    drop    pass numbers (0-based) that are missing
    """
    n = int(total_ms * FS_PER_MS)
    a = _colored(rng, n, noise_mv)
    pulse = _lobe_pair(amp=1.0)
    t_us = start_ms * 1000.0
    end_us = (end_ms if end_ms is not None else total_ms) * 1000.0
    k = 0
    while t_us < end_us - 200:
        if k not in drop:
            i = int(round(t_us / DT_US))
            g = amp * (1 + amp_var * rng.standard_normal())
            seg = g * pulse
            j0 = max(0, i)
            j1 = min(n, i + seg.size)
            if j1 > j0:
                a[j0:j1] += seg[: j1 - j0]
        frac = (t_us / 1000.0) / total_ms
        t_us += period_us * (1 + drift * frac) + jitter_us * rng.standard_normal()
        k += 1
    # mirror-switch transient inside the blanking window
    tt = np.arange(int(3.0 * FS_PER_MS))
    a[: tt.size] += 100.0 * np.exp(-tt / (0.6 * FS_PER_MS)) * np.sin(tt / 40.0)
    return a


# --------------------------------------------------------------------------
# real data
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def figure3():
    if not FIGURE_3.exists():
        pytest.skip("FIGURE 3 fixture missing")
    t, a, _b = load_pico_csv(FIGURE_3)
    dt_ns = float(np.median(np.diff(t))) * 1e6
    assert abs(dt_ns - DT_NS) < 1.0
    return a


@pytest.mark.parametrize("chunk_ms", [0.25, 0.5, 1.0, 5.0])
def test_figure3_confirms_and_is_never_released(figure3, chunk_ms):
    det, statuses = run(figure3, chunk_ms)
    final = statuses[-1]
    assert final.state == HOLDING, final
    assert all(s.state != RELEASED for s in statuses)
    # confirms soon after blanking ends, not at the deadline
    first_hold = next(s for s in statuses if s.state == HOLDING)
    assert first_hold.t_ms <= 3.5 + 4.0 + chunk_ms
    # tracked period matches the measured ~231 us, within the 235 -> 226 us drift
    assert 215.0 < final.period_us < 245.0
    # nearly every pass is hit; misses are isolated (never 5 in a row)
    assert final.hits > 150
    assert max(s.missed for s in statuses) <= 2
    assert final.total_missed <= 12


def test_figure3_result_is_chunk_size_independent(figure3):
    finals = [run(figure3, c)[1][-1] for c in (0.25, 1.0, 5.0)]
    assert len({(f.hits, f.total_missed) for f in finals}) == 1


def test_figure3_survives_the_noise_burst(figure3):
    """The ~11.8 ms burst (-66 mV) must neither release nor create a false catch
    track: the hold continues with the same period afterwards."""
    det, statuses = run(figure3, 0.5)
    before = next(s for s in statuses if s.t_ms >= 11.0)
    after = next(s for s in statuses if s.t_ms >= 13.0)
    assert after.state == HOLDING
    assert after.hits > before.hits
    assert abs(after.period_us - before.period_us) < 0.15 * before.period_us


def test_figure3_overrange_samples_are_handled(figure3):
    # the file contains overrange (NaN) samples at the very start
    assert np.isnan(figure3).any()
    det, statuses = run(figure3, 0.5)
    assert statuses[-1].state == HOLDING


# --------------------------------------------------------------------------
# synthetic behaviour
# --------------------------------------------------------------------------

def test_blanking_then_confirming_states():
    rng = np.random.default_rng(1)
    a = make_train(rng, 20.0)
    det = HoldDetector(DT_NS)
    st = det.feed(a[: int(1.0 * FS_PER_MS)])
    assert st.state == BLANKING
    st = det.feed(a[int(1.0 * FS_PER_MS): int(4.0 * FS_PER_MS)])
    assert st.state in (CONFIRMING, HOLDING)


def test_steady_train_holds_for_the_whole_record():
    rng = np.random.default_rng(2)
    det, sts = run(make_train(rng, 50.0), 0.5)
    assert sts[-1].state == HOLDING
    assert sts[-1].period_us == pytest.approx(231.0, rel=0.05)
    assert sts[-1].total_missed <= 4


def test_loss_releases_within_max_missed_periods():
    rng = np.random.default_rng(3)
    t_loss, period, chunk = 20.0, 231.0, 0.5
    cfg = HoldDetectorConfig()
    a = make_train(rng, 40.0, period_us=period, end_ms=t_loss)
    det, sts = run(a, chunk, cfg)
    last = sts[-1]
    assert last.state == RELEASED and last.reason == "lost"
    # last pass ends ~t_loss; detection = max_missed periods + filter margin + chunk
    expected = t_loss + (cfg.max_missed + 1) * period / 1000.0 + 0.8 + chunk
    assert t_loss < last.released_at_ms <= expected, last.released_at_ms


def test_noise_only_is_a_false_trigger():
    rng = np.random.default_rng(4)
    cfg = HoldDetectorConfig()
    a = _colored(rng, 30 * FS_PER_MS, 1.0)
    det, sts = run(a, 0.5, cfg)
    last = sts[-1]
    assert last.state == RELEASED and last.reason == "no_catch"
    deadline = cfg.blank_ms + cfg.confirm_deadline_ms
    assert deadline <= last.released_at_ms <= deadline + 0.6


def test_a_lone_burst_without_a_train_is_not_a_catch():
    rng = np.random.default_rng(5)
    a = _colored(rng, 30 * FS_PER_MS, 1.0)
    i = int(7.0 * FS_PER_MS)
    a[i:i + _lobe_pair().size] += 6.0 * _lobe_pair()      # one big pass-like burst
    det, sts = run(a, 0.5)
    assert sts[-1].state == RELEASED and sts[-1].reason == "no_catch"
    assert all(s.state != HOLDING for s in sts)


def test_irregular_bursts_are_not_a_catch():
    rng = np.random.default_rng(6)
    a = _colored(rng, 30 * FS_PER_MS, 1.0)
    pulse = _lobe_pair(9.0)
    t_ms = 4.0
    while t_ms < 20:
        i = int(t_ms * FS_PER_MS)
        a[i:i + pulse.size] += pulse
        t_ms += rng.uniform(0.15, 1.2)          # irregular spacing
    det, sts = run(a, 0.5)
    assert all(s.state != HOLDING for s in sts)


@pytest.mark.parametrize("drift", [0.04, -0.04])
def test_four_percent_period_drift_stays_held(drift):
    rng = np.random.default_rng(7)
    det, sts = run(make_train(rng, 50.0, drift=drift), 0.5)
    assert sts[-1].state == HOLDING


def test_single_and_short_runs_of_missed_passes_do_not_release():
    rng = np.random.default_rng(8)
    drops = {40, 60, 61, 100, 101, 102, 103}      # one, two and a four-in-a-row
    det, sts = run(make_train(rng, 50.0, drop=drops), 0.5)
    assert sts[-1].state == HOLDING
    # the four-in-a-row is ridden out (4 < max_missed; the widened window may
    # re-acquire on the 4th prediction, so 3 or 4 misses are both fine)
    assert 3 <= max(s.missed for s in sts) <= 4
    assert sts[-1].total_missed >= len(drops) - 2


def test_six_missed_passes_in_a_row_release():
    rng = np.random.default_rng(9)
    drops = set(range(60, 70))                    # well over max_missed
    det, sts = run(make_train(rng, 50.0, drop=drops), 0.5)
    assert sts[-1].state == RELEASED and sts[-1].reason == "lost"


def test_burst_during_hold_does_not_break_or_extend_it():
    rng = np.random.default_rng(10)
    a = make_train(rng, 50.0)
    i = int(15.0 * FS_PER_MS)
    burst = 8.0 * _lobe_pair(9.0)                 # 6x pass amplitude, off-phase
    a[i + 300:i + 300 + burst.size] += burst
    det, sts = run(a, 0.5)
    assert sts[-1].state == HOLDING

    # ... and a burst after the particle leaves does not keep the hold alive
    rng = np.random.default_rng(11)
    a = make_train(rng, 40.0, end_ms=20.0)
    j = int(22.0 * FS_PER_MS)
    a[j:j + burst.size] += burst
    det, sts = run(a, 0.5)
    assert sts[-1].state == RELEASED and sts[-1].reason == "lost"


def test_hold_max_caps_the_hold():
    rng = np.random.default_rng(12)
    cfg = HoldDetectorConfig(hold_max_s=0.030)
    det, sts = run(make_train(rng, 60.0), 0.5, cfg)
    last = sts[-1]
    assert last.state == RELEASED and last.reason == "max_hold"
    assert 30.0 <= last.released_at_ms <= 30.6


def test_overrange_gaps_are_interpolated():
    rng = np.random.default_rng(13)
    a = make_train(rng, 30.0)
    a[int(8.0 * FS_PER_MS): int(8.0 * FS_PER_MS) + 70] = np.nan
    a[:5] = np.inf
    det, sts = run(a, 0.5)
    assert sts[-1].state == HOLDING


def test_chunk_size_does_not_change_the_synthetic_outcome():
    rng = np.random.default_rng(14)
    a = make_train(rng, 40.0, end_ms=22.0)
    results = []
    for chunk in (0.25, 0.7, 3.0):
        det, sts = run(a, chunk)
        results.append((sts[-1].reason, round(sts[-1].released_at_ms)))
    reasons = {r for r, _ in results}
    assert reasons == {"lost"}
    times = [t for _, t in results]
    assert max(times) - min(times) <= 3          # differ only by chunk granularity


def test_released_detector_ignores_further_data_and_reset_works():
    rng = np.random.default_rng(15)
    a = _colored(rng, 30 * FS_PER_MS, 1.0)
    det, sts = run(a, 0.5)
    assert det.released
    again = det.feed(a[:1000])
    assert again.state == RELEASED and again.reason == "no_catch"
    det.reset()
    assert det.status().state == BLANKING


def test_bad_sample_interval_rejected():
    with pytest.raises(ValueError):
        HoldDetector(0)


def test_config_roundtrip_dict():
    d = HoldDetectorConfig(max_missed=7).to_dict()
    assert d["max_missed"] == 7 and d["blank_ms"] == 3.5
