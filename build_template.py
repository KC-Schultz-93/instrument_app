"""
build_template.py
-----------------
Builds a matched-filter template .npy file from PicoScope 7 single-waveform CSV exports.

Expected CSV format (one file per capture, as exported by PicoScope 7):
  Row 0:  "Time,Channel A"
  Row 1:  "(us),(mV)"          ← units row (skipped automatically)
  Row 2:  (blank, optional)
  Row 3+: time_us, voltage_mV

Usage
-----
  python build_template.py  "Signal samples/*.csv"  --out template.npy  [options]
   -- OR --
  python build_template.py  "Signal samples/"       --out template.npy  [options]

Options
-------
  --channel     A|B          Which channel column to use (default: A)
  --polarity    pos|neg|auto pos = positive lobe first (cation mode),
                             neg = negative lobe first (anion mode),
                             auto = detect per file (default: auto)
  --pulse-min   FLOAT        Minimum valid pulse duration in µs (default: 60)
  --pulse-max   FLOAT        Maximum valid pulse duration in µs (default: 110)
  --pad         FLOAT        Padding around extracted pulse in µs (default: 30)
  --min-snr     FLOAT        Minimum peak/RMS SNR to keep a file (default: 3.0)
  --plot                     Show alignment plot before saving (requires matplotlib)
  --out         PATH         Output .npy file (default: template.npy)

Output
------
  template.npy — 1-D float32 array, zero-mean, unit-max normalized, in mV scale.
                 Metadata printed to stdout.
"""

import argparse
import glob
import os
import sys
import numpy as np

# Windows consoles often default to a legacy codepage (e.g. cp1252) that can't
# encode the µ/→ characters used below; force UTF-8 stdout so printing never crashes.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------------------
# CSV loading — single-waveform format
# ---------------------------------------------------------------------------

def load_single_waveform_csv(path: str, channel: str = "A"):
    """
    Loads a PicoScope 7 single-waveform CSV.
    Returns (time_us, voltage_mV) as 1-D float64 arrays.

    Handles:
      - Optional blank lines
      - Units row (e.g. "(us),(mV)") on row 1 — skipped automatically
      - Comma or tab delimiters
    """
    time_vals = []
    volt_vals = []
    ch_label = f"channel {channel.lower()}"

    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()

    # Find header row (contains "Time" or "time")
    header_row = 0
    for i, line in enumerate(lines):
        if "time" in line.lower():
            header_row = i
            break

    # Determine channel column index from header
    header = lines[header_row].strip().replace("\t", ",").split(",")
    ch_col = None
    for i, h in enumerate(header):
        if ch_label in h.lower() or f"ch {channel.upper()}" in h:
            ch_col = i
            break
    if ch_col is None:
        # Fall back: second column
        ch_col = 1

    # Parse data rows (skip header, units row, blank rows)
    for line in lines[header_row + 1:]:
        line = line.strip()
        if not line:
            continue
        # Skip units row like "(us),(mV)"
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

    # Auto-detect if time is in seconds (convert to µs)
    if len(time_us) > 1:
        span = time_us[-1] - time_us[0]
        if abs(span) < 0.01:          # span < 0.01 → likely in seconds
            time_us *= 1e6

    # Auto-detect if voltage is in V (convert to mV)
    peak = np.max(np.abs(voltage_mv))
    if peak < 0.05:                   # peak < 50 mV → likely in V
        voltage_mv *= 1000.0

    return time_us, voltage_mv


# ---------------------------------------------------------------------------
# Signal quality
# ---------------------------------------------------------------------------

def estimate_snr(waveform: np.ndarray) -> float:
    rms = np.sqrt(np.mean(waveform ** 2))
    if rms == 0:
        return 0.0
    return float(np.max(np.abs(waveform)) / rms)


def measure_pulse_duration(waveform: np.ndarray, time_us: np.ndarray,
                           polarity: str) -> float:
    """
    Measures the full bipolar pulse duration as the time between the
    positive and negative lobe peaks.

    This is the 'half-period' in sine terms — the 78–92 µs value
    the user refers to as the signal period.

    Returns duration in µs, or NaN on failure.
    """
    pos_peak_idx = int(np.argmax(waveform))
    neg_peak_idx = int(np.argmin(waveform))

    if polarity == "pos":
        first_idx, second_idx = pos_peak_idx, neg_peak_idx
    elif polarity == "neg":
        first_idx, second_idx = neg_peak_idx, pos_peak_idx
    else:
        # auto: whichever peak comes first in time
        if pos_peak_idx < neg_peak_idx:
            first_idx, second_idx = pos_peak_idx, neg_peak_idx
        else:
            first_idx, second_idx = neg_peak_idx, pos_peak_idx

    if first_idx >= second_idx:
        return float("nan")

    return float(time_us[second_idx] - time_us[first_idx])


# ---------------------------------------------------------------------------
# Pulse extraction
# ---------------------------------------------------------------------------

def extract_pulse(waveform: np.ndarray, time_us: np.ndarray,
                  polarity: str, pad_us: float):
    """
    Extracts a padded window centered on the bipolar pulse.
    Searches backward from whichever lobe comes FIRST in time for the onset
    zero-crossing, and forward from whichever lobe comes LAST in time for the
    offset zero-crossing. Anchoring to the larger-magnitude peak instead (as
    before) cuts off the earlier lobe whenever it's the smaller one, because
    the backward search then stops at the inter-lobe crossing instead of
    continuing past it to the true onset.

    Returns (pulse_array, pulse_time_us) or (None, None).
    """
    pos_peak_idx = int(np.argmax(waveform))
    neg_peak_idx = int(np.argmin(waveform))

    # Always search backward from the FIRST lobe (earliest in time)
    # and forward from the SECOND lobe (latest in time)
    if pos_peak_idx < neg_peak_idx:
        first_idx, last_idx = pos_peak_idx, neg_peak_idx
    else:
        first_idx, last_idx = neg_peak_idx, pos_peak_idx

    dt_us = float(np.median(np.diff(time_us)))
    pad_samples = int(pad_us / dt_us)

    signs = np.sign(waveform)

    # Search backward from first lobe for onset zero crossing
    onset = first_idx
    for i in range(first_idx - 1, max(0, first_idx - 500), -1):
        if signs[i] != signs[first_idx] or waveform[i] == 0:
            onset = i
            break

    # Search forward from last lobe for return-to-baseline zero crossing
    offset = last_idx
    for i in range(last_idx + 1, min(len(waveform), last_idx + 500)):
        if signs[i] != signs[last_idx] or waveform[i] == 0:
            offset = i
            break

    start = max(0, onset - pad_samples)
    end = min(len(waveform), offset + pad_samples)

    if end - start < 10:
        return None, None

    return waveform[start:end], time_us[start:end]


# ---------------------------------------------------------------------------
# Alignment & averaging
# ---------------------------------------------------------------------------

def align_pulses(pulses):
    """
    Aligns pulses by cross-correlation against the pulse with the highest peak.
    Returns 2-D array (n_pulses, max_len), zero-padded.
    """
    ref_idx = int(np.argmax([np.max(np.abs(p)) for p in pulses]))
    ref = pulses[ref_idx]
    max_len = max(len(p) for p in pulses)

    aligned = []
    for p in pulses:
        # Pad p to at least ref length for correlation
        p_pad = np.pad(p, (0, max(0, len(ref) - len(p))))
        corr = np.correlate(ref, p_pad[:len(ref)], mode="full")
        shift = int(np.argmax(corr)) - (len(ref) - 1)

        if shift >= 0:
            shifted = np.pad(p, (shift, 0))
        else:
            shifted = p[-shift:]

        # Pad/trim to max_len
        if len(shifted) < max_len:
            shifted = np.pad(shifted, (0, max_len - len(shifted)))
        else:
            shifted = shifted[:max_len]

        aligned.append(shifted)

    return np.array(aligned, dtype=np.float64)


def normalize_template(template: np.ndarray) -> np.ndarray:
    t = template - np.mean(template)
    peak = np.max(np.abs(t))
    if peak == 0:
        raise ValueError("Template is all zeros after mean subtraction.")
    return (t / peak).astype(np.float32)


# ---------------------------------------------------------------------------
# Optional plot
# ---------------------------------------------------------------------------

def show_alignment_plot(aligned: np.ndarray, template: np.ndarray,
                        dt_us: float):
    try:
        import matplotlib.pyplot as plt
        import matplotlib as mpl
        mpl.rcParams.update({"axes.facecolor": "#1a1a2e",
                              "figure.facecolor": "#0f0e17",
                              "text.color": "white",
                              "axes.labelcolor": "white",
                              "xtick.color": "white",
                              "ytick.color": "white"})
    except ImportError:
        print("matplotlib not installed — skipping plot.")
        return

    t_ax = np.arange(aligned.shape[1]) * dt_us
    fig, axes = plt.subplots(1, 2, figsize=(13, 4))

    ax = axes[0]
    for row in aligned:
        ax.plot(t_ax, row, alpha=0.25, linewidth=0.8, color="#4fc3f7")
    ax.plot(t_ax, np.mean(aligned, axis=0), color="white",
            linewidth=2, label="Mean")
    ax.set_title(f"Aligned captures (n={len(aligned)})")
    ax.set_xlabel("Time relative to pulse start (µs)")
    ax.set_ylabel("Amplitude (mV)")
    ax.legend()

    ax = axes[1]
    t_tmpl = np.arange(len(template)) * dt_us
    ax.plot(t_tmpl, template, color="#4fc3f7", linewidth=2)
    ax.axhline(0, color="#555", linewidth=0.8)
    ax.set_title("Final normalized template")
    ax.set_xlabel("Sample index")
    ax.set_ylabel("Normalized amplitude")

    plt.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build matched-filter template from PicoScope 7 single-waveform CSVs."
    )
    parser.add_argument("input",
                        help='Folder path or glob pattern, e.g. "Signal samples/" '
                             'or "Signal samples/*.csv"')
    parser.add_argument("--channel", default="A", choices=["A", "B"])
    parser.add_argument("--polarity", default="auto",
                        choices=["pos", "neg", "auto"],
                        help="pos=positive first (cation), neg=negative first (anion)")
    parser.add_argument("--pulse-min", type=float, default=60.0,
                        help="Min valid pulse duration µs (default: 60)")
    parser.add_argument("--pulse-max", type=float, default=110.0,
                        help="Max valid pulse duration µs (default: 110)")
    parser.add_argument("--pad", type=float, default=30.0,
                        help="Padding around pulse in µs (default: 30)")
    parser.add_argument("--min-snr", type=float, default=3.0,
                        help="Min SNR to keep a file (default: 3.0)")
    parser.add_argument("--plot", action="store_true")
    parser.add_argument("--out", default="template.npy")
    args = parser.parse_args()

    # Collect CSV files
    inp = args.input
    if os.path.isdir(inp):
        csv_files = sorted(glob.glob(os.path.join(inp, "*.csv")))
    else:
        csv_files = sorted(glob.glob(inp))

    if not csv_files:
        print(f"ERROR: No CSV files found at: {inp}")
        sys.exit(1)

    print(f"Found {len(csv_files)} CSV file(s)")

    kept_pulses = []
    kept_durations = []
    dt_us_list = []
    rejected = {"snr": 0, "duration": 0, "extract": 0, "load": 0}

    for path in csv_files:
        fname = os.path.basename(path)
        try:
            time_us, voltage_mv = load_single_waveform_csv(path, args.channel)
        except Exception as e:
            print(f"  SKIP {fname}: load error — {e}")
            rejected["load"] += 1
            continue

        if len(time_us) < 10:
            rejected["load"] += 1
            continue

        dt_us = float(np.median(np.diff(time_us)))
        dt_us_list.append(dt_us)

        # Baseline subtract
        waveform = voltage_mv - np.median(voltage_mv)

        # SNR check
        snr = estimate_snr(waveform)
        if snr < args.min_snr:
            print(f"  SKIP {fname}: SNR {snr:.1f} < {args.min_snr}")
            rejected["snr"] += 1
            continue

        # Pulse duration check (peak-to-peak time = the 78–92 µs value)
        duration = measure_pulse_duration(waveform, time_us, args.polarity)
        if not (args.pulse_min <= duration <= args.pulse_max):
            print(f"  SKIP {fname}: duration {duration:.1f} µs outside "
                  f"[{args.pulse_min}, {args.pulse_max}]")
            rejected["duration"] += 1
            continue

        # Extract pulse window
        pulse, _ = extract_pulse(waveform, time_us, args.polarity, args.pad)
        if pulse is None:
            print(f"  SKIP {fname}: pulse extraction failed")
            rejected["extract"] += 1
            continue

        kept_pulses.append(pulse)
        kept_durations.append(duration)
        print(f"  OK   {fname}: SNR={snr:.1f}, duration={duration:.1f} µs")

    print(f"\nResults:")
    print(f"  Kept:                {len(kept_pulses)}")
    print(f"  Rejected (SNR):      {rejected['snr']}")
    print(f"  Rejected (duration): {rejected['duration']}")
    print(f"  Rejected (extract):  {rejected['extract']}")
    print(f"  Rejected (load):     {rejected['load']}")

    if len(kept_pulses) < 3:
        print("\nERROR: Too few valid captures. Try:")
        print("  --min-snr 2.0  --pulse-min 50  --pulse-max 120")
        sys.exit(1)

    dt_us = float(np.median(dt_us_list))
    sample_rate_hz = 1e6 / dt_us

    print(f"\nAligning {len(kept_pulses)} pulses ...")
    aligned = align_pulses(kept_pulses)

    mean_dur = float(np.mean(kept_durations))
    print(f"  Mean pulse duration: {mean_dur:.1f} µs "
          f"(range {min(kept_durations):.1f}–{max(kept_durations):.1f} µs)")
    print(f"  Sample interval:     {dt_us:.4f} µs → {sample_rate_hz/1e6:.3f} MHz")

    template_raw = np.mean(aligned, axis=0)
    template = normalize_template(template_raw)

    if args.plot:
        show_alignment_plot(aligned, template, dt_us)

    np.save(args.out, template)

    print(f"\nTemplate saved → {args.out}")
    print(f"  Shape:       {template.shape}")
    print(f"  Dtype:       {template.dtype}")
    print(f"  Duration:    {len(template) * dt_us:.1f} µs")
    print(f"  Sample rate: {sample_rate_hz:.0f} Hz")
    print()
    print("In matched_filter_config:")
    print(f"  use_empirical_template  = True")
    print(f"  empirical_template_path = '{args.out}'")


if __name__ == "__main__":
    main()