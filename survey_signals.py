"""
survey_signals.py
-----------------
Surveys a folder of PicoScope 7 single-waveform CSV captures, keeps only
waveforms containing a bipolar pulse (one +lobe and one -lobe), measures each
pulse, groups them with k-means, and writes a ranked report, a scatter plot,
and a sorted folder of files ready to feed into build_template.py.

Expected CSV format is the same as build_template.py:
  Row 0:  "Time,Channel A"
  Row 1:  "(us),(mV)"
  Row 2:  (blank, optional)
  Row 3+: time_us, voltage_mV

Usage
-----
  python survey_signals.py "Signal samples/"  [options]
  python survey_signals.py "Signal samples/*.csv" --out-dir survey_out

What it does, per file
----------------------
  1. Subtract the median baseline and lightly smooth (--smooth-us).
  2. Estimate noise robustly (median of per-block MADs, so the pulse itself
     doesn't inflate it).
  3. Find every prominent + and - lobe, then pair neighbouring opposite-sign
     lobes into bipolar events. A pair is accepted when
       - both lobes reach at least --snr-min x noise (and are prominent),
       - the smaller lobe is >= --ratio-min x the larger one,
       - the lobe-to-lobe time is within [--dur-min, --dur-max] us.
     Files with no accepted pair are rejected (reason is recorded).
     A file with several pulses yields several events (each its own row).
  4. Amplitude = the LARGER of the two lobes.  Duration = time between the
     + and - peaks.  Events whose amplitude is outside
     [--amp-min, --amp-max] are kept but marked out-of-window.
  5. In-window events are clustered with k-means on (log amplitude,
     log duration), standardized, with amplitude down-weighted (--amp-weight,
     default 0.5) because the matched filter's normalized correlation already
     handles amplitude and duration is what changes the pulse shape.  k is
     chosen automatically by silhouette score unless --k is given (fewer than
     6 in-window events, or weak structure, gives a single cluster).
     Clusters are numbered by median duration, short to long.

Outputs (in --out-dir, default survey_out/)
-------------------------------------------
  report.csv      one row per bipolar event, ranked (amplitude first)
  rejected.csv    one row per rejected file with the reason
  scatter.png     amplitude vs duration, coloured by cluster
  sorted/         cluster_1/, cluster_2/, ... and out_of_window/
                  Files are prefixed with a rank, e.g.
                  003_0.213mV_52us_<name>.csv
                  Single-event files are copied as-is.  For files with
                  several pulses, each pulse is written as its own padded
                  snippet CSV (same format) so build_template.py can use it.

Dependencies: numpy, scipy, matplotlib; scikit-learn for clustering (if it is
missing the script still runs, with everything in a single cluster).
"""

import argparse
import csv
import glob
import os
import re
import shutil
import sys

import numpy as np
from scipy.signal import find_peaks

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------------------
# CSV loading (same logic as build_template.py)
# ---------------------------------------------------------------------------

def load_single_waveform_csv(path: str, channel: str = "A"):
    time_vals, volt_vals = [], []
    ch_label = f"channel {channel.lower()}"

    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()

    header_row = 0
    for i, line in enumerate(lines):
        if "time" in line.lower():
            header_row = i
            break

    header = lines[header_row].strip().replace("\t", ",").split(",")
    ch_col = None
    for i, h in enumerate(header):
        if ch_label in h.lower() or f"ch {channel.upper()}" in h:
            ch_col = i
            break
    if ch_col is None:
        ch_col = 1

    for line in lines[header_row + 1:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("(") or line.lower().startswith("us"):
            continue
        parts = line.replace("\t", ",").split(",")
        try:
            t = float(parts[0])
            v = float(parts[ch_col])
        except (ValueError, IndexError):
            continue
        time_vals.append(t)
        volt_vals.append(v)

    time_us = np.array(time_vals, dtype=np.float64)
    voltage_mv = np.array(volt_vals, dtype=np.float64)

    if len(time_us) > 1:
        span = time_us[-1] - time_us[0]
        if abs(span) < 0.01:          # time in seconds -> µs
            time_us *= 1e6
    if len(voltage_mv) and np.max(np.abs(voltage_mv)) < 0.05:   # volts -> mV
        voltage_mv *= 1000.0

    return time_us, voltage_mv


def write_waveform_csv(path: str, time_us: np.ndarray, voltage_mv: np.ndarray):
    """Write in the same PicoScope-7 layout the other scripts read."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("Time,Channel A\n(us),(mV)\n\n")
        for t, v in zip(time_us, voltage_mv):
            f.write(f"{t:.8f},{v:.8f}\n")


# ---------------------------------------------------------------------------
# Signal processing
# ---------------------------------------------------------------------------

def smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return x.copy()
    k = np.ones(n) / n
    pad = n // 2
    xp = np.pad(x, (pad, n - 1 - pad), mode="edge")
    return np.convolve(xp, k, mode="valid")


def estimate_noise(w: np.ndarray, n_blocks: int = 10) -> float:
    """
    Robust noise sigma: median over blocks of each block's MAD-sigma.
    Pulses occupy only a few blocks, so the median ignores them.
    """
    n = len(w)
    if n < n_blocks * 10:
        return float(1.4826 * np.median(np.abs(w - np.median(w))))
    edges = np.linspace(0, n, n_blocks + 1, dtype=int)
    sig = []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = w[a:b]
        sig.append(1.4826 * np.median(np.abs(seg - np.median(seg))))
    return float(np.median(sig))


def find_lobes(ws: np.ndarray, prominence: float, min_height: float):
    """Return alternating-sign lobes [(idx, sign, value)] sorted by time.
    A lobe must be prominent AND reach min_height from the baseline, so slow
    noise wobbles that are only prominent against each other are ignored."""
    pos, _ = find_peaks(ws, prominence=prominence)
    neg, _ = find_peaks(-ws, prominence=prominence)
    lobes = [(int(i), +1, float(ws[i])) for i in pos if ws[i] >= min_height]
    lobes += [(int(i), -1, float(ws[i])) for i in neg if ws[i] <= -min_height]
    lobes.sort(key=lambda L: L[0])

    # merge consecutive same-sign lobes, keeping the larger
    merged = []
    for L in lobes:
        if merged and merged[-1][1] == L[1]:
            if abs(L[2]) > abs(merged[-1][2]):
                merged[-1] = L
        else:
            merged.append(L)
    return merged


def extract_window(ws, i_first, i_last, dt, pad_us):
    """Zero-crossing onset/offset around the pulse, plus padding (as in
    build_template.extract_pulse). Returns (start, end) sample indices."""
    signs = np.sign(ws)
    onset = i_first
    for i in range(i_first - 1, max(0, i_first - 500), -1):
        if signs[i] != signs[i_first] or ws[i] == 0:
            onset = i
            break
    offset = i_last
    for i in range(i_last + 1, min(len(ws), i_last + 500)):
        if signs[i] != signs[i_last] or ws[i] == 0:
            offset = i
            break
    pad = int(pad_us / dt)
    return max(0, onset - pad), min(len(ws), offset + pad)


def analyse_waveform(time_us, voltage_mv, args):
    """
    Returns (events, reject_reason, info).
    events: list of dicts (bipolar events found).
    reject_reason: None if at least one event was found, else a string.
    """
    dt = float(np.median(np.diff(time_us)))
    w = voltage_mv - np.median(voltage_mv)
    n_smooth = max(1, int(round(args.smooth_us / dt)))
    ws = smooth(w, n_smooth)

    sigma = estimate_noise(ws)
    sigma = max(sigma, 1e-9)
    prom = 0.5 * args.snr_min * sigma     # height gate (snr_min x sigma) does the heavy lifting

    lobes = find_lobes(ws, prom, args.snr_min * sigma)
    info = {"noise_mv": sigma, "n_lobes": len(lobes), "dt_us": dt}

    if len(lobes) == 0:
        return [], f"no lobe above {args.snr_min:g}x noise ({sigma:.3f} mV)", info
    if len(lobes) == 1:
        return [], "only one lobe (not bipolar)", info

    # Evaluate every neighbouring opposite-sign pair
    candidates = []
    for k in range(len(lobes) - 1):
        a, b = lobes[k], lobes[k + 1]
        amp_a, amp_b = abs(a[2]), abs(b[2])
        big, small = max(amp_a, amp_b), min(amp_a, amp_b)
        sep = (b[0] - a[0]) * dt
        ratio = small / big
        if not (args.dur_min <= sep <= args.dur_max):
            status = f"lobe separation {sep:.0f} us outside [{args.dur_min:g}, {args.dur_max:g}]"
        elif ratio < args.ratio_min:
            status = f"lobes unequal (ratio {ratio:.2f} < {args.ratio_min:g})"
        else:
            pol = "pos_first" if a[1] > 0 else "neg_first"
            if args.polarity == "pos" and pol != "pos_first":
                status = "polarity filter (neg first)"
            elif args.polarity == "neg" and pol != "neg_first":
                status = "polarity filter (pos first)"
            else:
                status = "ok"
        candidates.append(dict(k=k, a=a, b=b, big=big, small=small,
                               sep=sep, ratio=ratio, status=status))

    ok = [c for c in candidates if c["status"] == "ok"]
    if not ok:
        best = max(candidates, key=lambda c: c["small"])
        return [], best["status"], info

    # Choose non-overlapping pairs. In a sequence like + - + - the middle pair
    # (- +) is also a valid opposite-sign pair, so a greedy "strongest first"
    # choice can steal lobes from the real events. Instead pick, by dynamic
    # programming, the pairing with the most events, then the highest total
    # strength (sum of the smaller lobe of each pair).
    ok_by_k = {c["k"]: c for c in ok}
    n_l = len(lobes)
    best = [(0, 0.0, None)] * (n_l + 2)       # best[i] = (count, score, pair_k) for lobes i..
    for i in range(n_l - 1, -1, -1):
        skip = (best[i + 1][0], best[i + 1][1], None)
        if i in ok_by_k:
            take = (1 + best[i + 2][0], ok_by_k[i]["small"] + best[i + 2][1], i)
            best[i] = take if (take[0], take[1]) > (skip[0], skip[1]) else skip
        else:
            best[i] = skip
    chosen, i = [], 0
    while i < n_l:
        if best[i][2] is not None:
            chosen.append(ok_by_k[i])
            i += 2
        else:
            i += 1
    info["n_unpaired_lobes"] = len(lobes) - 2 * len(chosen)

    events = []
    for n, c in enumerate(chosen, 1):
        a, b = c["a"], c["b"]
        start, end = extract_window(ws, a[0], b[0], dt, args.pad)
        pos_lobe = a if a[1] > 0 else b
        neg_lobe = b if a[1] > 0 else a
        events.append(dict(
            event=n,
            amp_mv=c["big"],
            pos_peak_mv=pos_lobe[2],
            neg_peak_mv=neg_lobe[2],
            duration_us=c["sep"],
            ratio=c["ratio"],
            polarity="pos_first" if a[1] > 0 else "neg_first",
            t_first_us=float(time_us[a[0]]),
            t_second_us=float(time_us[b[0]]),
            snr=c["big"] / sigma,
            win=(start, end),
            _i_first=a[0], _i_second=b[0],
        ))

    # Keep snippet windows from running into a neighbouring pulse: clip at the
    # midpoint between consecutive events (so a global argmax/argmin on a
    # snippet, as build_template.py does, still lands on its own pulse).
    for prev, nxt in zip(events[:-1], events[1:]):
        mid = (prev["_i_second"] + nxt["_i_first"]) // 2
        prev["win"] = (prev["win"][0], min(prev["win"][1], mid))
        nxt["win"] = (max(nxt["win"][0], mid), nxt["win"][1])
    return events, None, info


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster_events(events, k_arg, amp_weight=0.5):
    """
    Cluster in-window events. Returns (labels, k, note, centroid_distance).
    labels are 1-based, ordered by median duration.
    """
    n = len(events)
    zeros = np.zeros(n)
    if n < 6 and not k_arg:
        return np.ones(n, dtype=int), 1, f"only {n} in-window events: single cluster", zeros
    try:
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
    except ImportError:
        return np.ones(n, dtype=int), 1, "scikit-learn missing: single cluster", zeros

    X = np.column_stack([np.log([e["amp_mv"] for e in events]),
                         np.log([e["duration_us"] for e in events])])
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - X.mean(axis=0)) / sd
    # Duration sets the pulse *shape* (and so which template fits); amplitude is
    # already absorbed by the normalized correlation. Down-weight amplitude so
    # clusters follow duration rather than splitting a continuous amplitude spread.
    Z[:, 0] *= amp_weight

    if k_arg:
        k = max(1, min(k_arg, n))
        note = f"k = {k} (set by --k)"
    else:
        scores = {}
        for kk in range(2, min(6, n - 1) + 1):
            lab = KMeans(n_clusters=kk, n_init=10, random_state=0).fit_predict(Z)
            if len(set(lab)) > 1:
                scores[kk] = silhouette_score(Z, lab)
        if not scores:
            return np.ones(n, dtype=int), 1, "clustering not possible: single cluster", zeros
        k = max(scores, key=scores.get)
        s = ", ".join(f"k={kk}: {v:.2f}" for kk, v in scores.items())
        if scores[k] < 0.40:
            return (np.ones(n, dtype=int), 1,
                    f"no clear cluster structure (silhouette {s}): single cluster", zeros)
        note = f"k = {k} chosen by silhouette ({s})"

    if k == 1:
        return np.ones(n, dtype=int), 1, note, zeros

    km = KMeans(n_clusters=k, n_init=10, random_state=0).fit(Z)
    raw = km.labels_
    dist = np.linalg.norm(Z - km.cluster_centers_[raw], axis=1)

    # renumber clusters by median duration (short -> long)
    durs = np.array([e["duration_us"] for e in events])
    order = sorted(range(k), key=lambda c: np.median(durs[raw == c]))
    remap = {old: new + 1 for new, old in enumerate(order)}
    labels = np.array([remap[c] for c in raw])
    return labels, k, note, dist


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def make_plot(path, in_win, out_win, labels, args, k, note):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    palette = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9"]
    volt_colors = {"5 kV": "#0072B2", "6 kV": "#E69F00", "7 kV": "#D55E00", "other": "#7a7a7a"}
    markers = ["o", "s", "^", "D", "v", "P"]
    fig, ax = plt.subplots(figsize=(9, 6), dpi=130)

    ax.axhspan(args.amp_min, args.amp_max, color="#999999", alpha=0.12,
               label=f"target window {args.amp_min:g}-{args.amp_max:g} mV")

    if args.color_by == "voltage":
        # colour = voltage group, marker shape = cluster, x = outside window
        for g, col in volt_colors.items():
            ow = [e for e in out_win if e["voltage"] == g]
            if ow:
                ax.scatter([e["duration_us"] for e in ow], [e["amp_mv"] for e in ow],
                           s=28, color=col, marker="x", alpha=0.6,
                           label=f"{g}, outside window (n={len(ow)})")
            for c in range(1, k + 1):
                pts = [e for e, l in zip(in_win, labels) if l == c and e["voltage"] == g]
                if not pts:
                    continue
                ax.scatter([e["duration_us"] for e in pts], [e["amp_mv"] for e in pts],
                           s=46, color=col, marker=markers[(c - 1) % len(markers)],
                           edgecolor="white", linewidth=0.6,
                           label=f"{g}" + (f", cluster {c}" if k > 1 else "") + f" (n={len(pts)})")
    else:
        if out_win:
            ax.scatter([e["duration_us"] for e in out_win],
                       [e["amp_mv"] for e in out_win],
                       s=28, color="#9a9a9a", marker="x", label="bipolar, outside window")

        for c in range(1, k + 1):
            pts = [e for e, l in zip(in_win, labels) if l == c]
            if not pts:
                continue
            ax.scatter([e["duration_us"] for e in pts], [e["amp_mv"] for e in pts],
                       s=46, color=palette[(c - 1) % len(palette)],
                       edgecolor="white", linewidth=0.6,
                       label=f"cluster {c} (n={len(pts)})" if k > 1 else f"in window (n={len(pts)})")
    for e in in_win:
        ax.annotate(str(e["rank"]), (e["duration_us"], e["amp_mv"]),
                    textcoords="offset points", xytext=(5, 4), fontsize=7)

    from matplotlib.ticker import FuncFormatter, LogLocator
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 3, 5)))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.yaxis.set_minor_formatter(FuncFormatter(lambda v, _: ""))
    ax.set_xlabel("Peak-to-peak time (µs)")
    ax.set_ylabel("Amplitude, larger lobe (mV)")
    ax.set_title("Bipolar events: amplitude vs duration", loc="left", fontsize=11, pad=20)
    ax.text(0, 1.015, note, transform=ax.transAxes, fontsize=7.5, color="#555555", va="bottom")
    ax.grid(True, which="both", alpha=0.25, linewidth=0.5)
    ax.legend(fontsize=8, frameon=False, loc="best")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Survey PicoScope CSVs: find bipolar pulses, rank, cluster, sort.")
    ap.add_argument("input", help='Folder or glob, e.g. "Signal samples/" or "Signal samples/*.csv"')
    ap.add_argument("--out-dir", default="survey_out")
    ap.add_argument("--channel", default="A", choices=["A", "B"])
    ap.add_argument("--amp-min", type=float, default=0.05, help="mV, larger lobe (default 0.05)")
    ap.add_argument("--amp-max", type=float, default=0.60, help="mV, larger lobe (default 0.60)")
    ap.add_argument("--dur-min", type=float, default=20.0, help="min lobe-to-lobe µs (default 20)")
    ap.add_argument("--dur-max", type=float, default=500.0, help="max lobe-to-lobe µs (default 500)")
    ap.add_argument("--polarity", default="auto", choices=["pos", "neg", "auto"],
                    help="pos = + lobe first only, neg = - lobe first only (default auto)")
    ap.add_argument("--ratio-min", type=float, default=0.5,
                    help="smaller lobe / larger lobe minimum (default 0.5)")
    ap.add_argument("--snr-min", type=float, default=4.0,
                    help="each lobe must reach this many noise sigmas (default 4)")
    ap.add_argument("--smooth-us", type=float, default=2.0,
                    help="moving-average width for detection, µs (default 2)")
    ap.add_argument("--pad", type=float, default=30.0, help="padding for snippets, µs (default 30)")
    ap.add_argument("--k", type=int, default=0, help="number of clusters (0 = automatic)")
    ap.add_argument("--amp-weight", type=float, default=0.5,
                    help="weight of amplitude vs duration in k-means features "
                         "(1 = equal, default 0.5)")
    ap.add_argument("--color-by", default="voltage", choices=["voltage", "cluster"],
                    help="scatter colouring: voltage = 5/6/7 kV/other from the filename "
                         "prefix (marker shape = cluster); cluster = colour by cluster")
    ap.add_argument("--ascending", action="store_true",
                    help="rank smallest amplitude first (default: largest first)")
    ap.add_argument("--no-copy", action="store_true", help="skip creating the sorted/ folder")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    inp = args.input
    if os.path.isdir(inp):
        files = sorted(glob.glob(os.path.join(inp, "*.csv")))
    else:
        files = sorted(glob.glob(inp))
    if not files:
        print(f"ERROR: no CSV files found at: {inp}")
        sys.exit(1)
    print(f"Found {len(files)} CSV file(s)")

    events, rejected = [], []
    waveforms = {}          # path -> (time_us, voltage_mv) for snippet export

    for path in files:
        fname = os.path.basename(path)
        try:
            t, v = load_single_waveform_csv(path, args.channel)
        except Exception as e:
            rejected.append(dict(file=fname, reason=f"load error: {e}", noise_mv=""))
            continue
        if len(t) < 50:
            rejected.append(dict(file=fname, reason="too few samples", noise_mv=""))
            continue

        evs, reason, info = analyse_waveform(t, v, args)
        if not evs:
            rejected.append(dict(file=fname, reason=reason,
                                 noise_mv=f"{info['noise_mv']:.4f}"))
            continue
        waveforms[path] = (t, v)
        for e in evs:
            e.update(file=fname, path=path, n_events_in_file=len(evs),
                     noise_mv=info["noise_mv"],
                     unpaired_lobes=info.get("n_unpaired_lobes", 0))
            events.append(e)

    # Voltage group from the filename (e.g. "6kV_signal_..." -> "6 kV")
    for e in events:
        m = re.match(r"(\d+)\s*kV", e["file"], re.IGNORECASE)
        g = f"{m.group(1)} kV" if m else "other"
        e["voltage"] = g if g in ("5 kV", "6 kV", "7 kV") else "other"

    # Amplitude window
    for e in events:
        if e["amp_mv"] < args.amp_min:
            e["status"] = "below_window"
        elif e["amp_mv"] > args.amp_max:
            e["status"] = "above_window"
        else:
            e["status"] = "in_window"
    in_win = [e for e in events if e["status"] == "in_window"]
    out_win = [e for e in events if e["status"] != "in_window"]

    # Rank: amplitude first (largest first unless --ascending), ties -> duration
    sign = 1 if args.ascending else -1
    in_win.sort(key=lambda e: (sign * e["amp_mv"], e["duration_us"]))
    for i, e in enumerate(in_win, 1):
        e["rank"] = i
    out_win.sort(key=lambda e: (sign * e["amp_mv"], e["duration_us"]))
    for i, e in enumerate(out_win, 1):
        e["rank"] = ""

    # Cluster
    if in_win:
        labels, k, note, dist = cluster_events(in_win, args.k, args.amp_weight)
    else:
        labels, k, note, dist = np.array([], dtype=int), 0, "no in-window events", np.array([])
    for e, l, d in zip(in_win, labels, dist):
        e["cluster"] = int(l)
        e["centroid_dist"] = float(d)
    for e in out_win:
        e["cluster"] = ""
        e["centroid_dist"] = ""

    # Within-cluster rank (by the same amplitude order)
    counts = {}
    for e in in_win:
        counts[e["cluster"]] = counts.get(e["cluster"], 0) + 1
        e["cluster_rank"] = counts[e["cluster"]]
    for e in out_win:
        e["cluster_rank"] = ""

    # Sorted folder
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    if not args.no_copy:
        sorted_dir = os.path.join(out_dir, "sorted")
        if os.path.isdir(sorted_dir):
            shutil.rmtree(sorted_dir)
        os.makedirs(sorted_dir)

        def dest_name(e, idx):
            stem = os.path.splitext(e["file"])[0]
            ev = f"_ev{e['event']}" if e["n_events_in_file"] > 1 else ""
            return f"{idx:03d}_{e['amp_mv']:.3f}mV_{e['duration_us']:.0f}us_{stem}{ev}.csv"

        def place(e, folder, idx):
            os.makedirs(folder, exist_ok=True)
            name = dest_name(e, idx)
            e["sorted_file"] = os.path.relpath(os.path.join(folder, name), out_dir)
            dst = os.path.join(folder, name)
            if e["n_events_in_file"] == 1:
                shutil.copyfile(e["path"], dst)
            else:
                t, v = waveforms[e["path"]]
                s, en = e["win"]
                write_waveform_csv(dst, t[s:en], v[s:en])

        for e in in_win:
            place(e, os.path.join(sorted_dir, f"cluster_{e['cluster']}"), e["cluster_rank"])
        for i, e in enumerate(out_win, 1):
            place(e, os.path.join(sorted_dir, "out_of_window"), i)
    for e in events:
        e.setdefault("sorted_file", "")

    # Report CSV
    cols = ["rank", "cluster", "cluster_rank", "file", "event", "n_events_in_file",
            "status", "amp_mv", "pos_peak_mv", "neg_peak_mv", "duration_us",
            "polarity", "lobe_ratio", "snr", "centroid_dist", "noise_mv",
            "unpaired_lobes", "t_first_us", "t_second_us", "sorted_file"]
    ordered = in_win + out_win
    with open(os.path.join(out_dir, "report.csv"), "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for e in ordered:
            wr.writerow([
                e["rank"], e["cluster"], e["cluster_rank"], e["file"], e["event"],
                e["n_events_in_file"], e["status"],
                f"{e['amp_mv']:.4f}", f"{e['pos_peak_mv']:.4f}", f"{e['neg_peak_mv']:.4f}",
                f"{e['duration_us']:.1f}", e["polarity"], f"{e['ratio']:.2f}",
                f"{e['snr']:.1f}",
                f"{e['centroid_dist']:.3f}" if e["centroid_dist"] != "" else "",
                f"{e['noise_mv']:.4f}", e["unpaired_lobes"],
                f"{e['t_first_us']:.1f}", f"{e['t_second_us']:.1f}", e["sorted_file"]])

    with open(os.path.join(out_dir, "rejected.csv"), "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["file", "reason", "noise_mv"])
        for r in rejected:
            wr.writerow([r["file"], r["reason"], r["noise_mv"]])

    if not args.no_plot and events:
        make_plot(os.path.join(out_dir, "scatter.png"), in_win, out_win,
                  labels, args, max(k, 1), note)

    # Summary
    n_files_bip = len({e["path"] for e in events})
    print(f"\nBipolar events: {len(events)} in {n_files_bip} file(s); "
          f"rejected files: {len(rejected)}")
    print(f"  In window ({args.amp_min:g}-{args.amp_max:g} mV): {len(in_win)}")
    print(f"  Below window: {sum(e['status'] == 'below_window' for e in events)}")
    print(f"  Above window: {sum(e['status'] == 'above_window' for e in events)}")
    print(f"Clustering: {note}")
    multi = sorted({e["file"] for e in events if e["n_events_in_file"] > 1})
    if multi:
        print(f"Multi-pulse files: {', '.join(multi)}")
    if in_win:
        print("\nRanked in-window events:")
        print(f"  {'rank':>4} {'cl':>2} {'amp(mV)':>8} {'dur(us)':>8} {'pol':>9}  file")
        for e in in_win:
            ev = f" [ev{e['event']}]" if e["n_events_in_file"] > 1 else ""
            print(f"  {e['rank']:>4} {e['cluster']:>2} {e['amp_mv']:>8.3f} "
                  f"{e['duration_us']:>8.1f} {e['polarity']:>9}  {e['file']}{ev}")
    if rejected:
        print("\nRejected files:")
        for r in rejected:
            print(f"  {r['file']}: {r['reason']}")
    print(f"\nOutputs in: {os.path.abspath(out_dir)}")


if __name__ == "__main__":
    main()
