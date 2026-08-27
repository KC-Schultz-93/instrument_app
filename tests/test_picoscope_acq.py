"""
tests/test_picoscope_acq.py
---------------------------
Minimal standalone acquisition test for PicoScope 4262.
Run directly with:  python tests/test_picoscope_acq.py

Prints step-by-step progress so you can see exactly where the
hardware pipeline stalls or produces wrong data, independently of the GUI.

When the signal saturates a range, the script automatically steps up to the
next larger range so you can see the actual DC offset and decide which
range to configure in the ratemeter page.
"""

import ctypes
import os
import sys
import time

import numpy as np

# ── DLL discovery (mirrors PicoScopeService._get_ps) ──────────────────────
if sys.platform == "win32":
    candidates = [
        r"C:\Program Files\Pico Technology\PicoScope 7 T&M Stable",
        r"C:\Program Files\Pico Technology\SDK\lib",
        r"C:\Program Files (x86)\Pico Technology\SDK\lib",
    ]
    for d in candidates:
        if os.path.isdir(d):
            if d not in os.environ.get("PATH", ""):
                os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
            try:
                os.add_dll_directory(d)
            except OSError:
                pass

from picosdk.ps4000 import ps4000 as ps

# ── Constants ──────────────────────────────────────────────────────────────
ADC_MAX      = 32512
CHANNEL_A    = 0
COUPLING_DC  = 1
TIMEBASE     = 23       # (23-2)*8 + 32 = 200 ns/sample
NUM_SAMPLES  = 5_000    # 5000 × 200 ns = 1 ms window
AUTOTRIG_MS  = 2000     # 2-second safety auto-trigger

# PS4000_RANGE enum: (enum_value, full-scale_volts, label)
RANGES = [
    (0,  0.010, "±10 mV"),
    (1,  0.020, "±20 mV"),
    (2,  0.050, "±50 mV"),
    (3,  0.100, "±100 mV"),
    (4,  0.200, "±200 mV"),
    (5,  0.500, "±500 mV"),
    (6,  1.000, "±1 V"),
    (7,  2.000, "±2 V"),
    (8,  5.000, "±5 V"),
    (9,  10.00, "±10 V"),
    (10, 20.00, "±20 V"),
]

SAMPLE_NS = (TIMEBASE - 2) * 8 + 32  # 200 ns

handle = ctypes.c_int16(0)


def check(status, label):
    code = status.value if hasattr(status, "value") else int(status)
    if code != 0:
        sys.exit(f"\nFatal SDK error in {label!r}: {code:#010x}")


def acquire(range_enum):
    """Run one block acquisition and return the raw int16 array."""
    # SetChannel
    for ch in range(2):
        check(ps.ps4000SetChannel(
            handle, ch,
            1 if ch == CHANNEL_A else 0,
            COUPLING_DC if ch == CHANNEL_A else 1,
            range_enum if ch == CHANNEL_A else 7,
        ), f"ps4000SetChannel(ch={ch})")

    # Disable trigger with auto-trigger safety
    check(ps.ps4000SetSimpleTrigger(handle, 0, 0, 0, 2, 0, AUTOTRIG_MS),
          "ps4000SetSimpleTrigger")

    # Cancel any leftover acquisition
    ps.ps4000Stop(handle)

    # RunBlock
    check(ps.ps4000RunBlock(
        handle, 0, NUM_SAMPLES, TIMEBASE, 1, None, 0, None, None,
    ), "ps4000RunBlock")

    # Poll until ready
    ready = ctypes.c_int16(0)
    for i in range(15_000):
        ps.ps4000IsReady(handle, ctypes.byref(ready))
        if ready.value:
            break
        if i % 1000 == 0 and i > 0:
            print(f"    ... still waiting at {i} ms")
        time.sleep(0.001)
    else:
        sys.exit("TIMEOUT: ps4000IsReady never returned 1 after 15 s.")

    # Register buffers and fetch
    buf     = (ctypes.c_int16 * NUM_SAMPLES)()
    buf_min = (ctypes.c_int16 * NUM_SAMPLES)()
    check(ps.ps4000SetDataBuffers(
        handle, CHANNEL_A, ctypes.byref(buf), ctypes.byref(buf_min), NUM_SAMPLES,
    ), "ps4000SetDataBuffers")

    overflow = ctypes.c_int16(0)
    n_values = ctypes.c_int32(NUM_SAMPLES)
    check(ps.ps4000GetValues(
        handle, 0, ctypes.byref(n_values), 1, 0, 0, ctypes.byref(overflow),
    ), "ps4000GetValues")

    raw = np.frombuffer(buf, dtype=np.int16, count=n_values.value).astype(float)
    return raw, bool(overflow.value)


# ── Open ───────────────────────────────────────────────────────────────────
print("Opening PicoScope 4262...")
status = ps.ps4000OpenUnit(ctypes.byref(handle))
check(status, "ps4000OpenUnit")
print(f"  Handle = {handle.value}")

# ── Range sweep ────────────────────────────────────────────────────────────
print(f"\nSweeping voltage ranges (DC coupling, {SAMPLE_NS} ns/sample)...")
print(f"{'Range':<12}  {'Mean (mV)':>12}  {'Std (mV)':>10}  {'Min (mV)':>10}  {'Max (mV)':>10}  Overflow  Saturated?")
print("-" * 80)

good_range = None
for enum, full_scale_v, label in RANGES:
    raw, overflow = acquire(enum)
    voltage_mv = raw / ADC_MAX * full_scale_v * 1000.0

    saturated = (abs(raw.max()) >= 32500) or (abs(raw.min()) >= 32500)
    flag = "YES ← clipped" if saturated else "no"
    ovf  = "YES" if overflow else "no"

    print(f"{label:<12}  {voltage_mv.mean():>12.3f}  {voltage_mv.std():>10.3f}"
          f"  {voltage_mv.min():>10.3f}  {voltage_mv.max():>10.3f}  {ovf:<8}  {flag}")

    if not saturated:
        good_range = (enum, full_scale_v, label, voltage_mv)
        break

print()

# ── Diagnosis ──────────────────────────────────────────────────────────────
if good_range is None:
    print("** Signal saturates even at ±20 V. Something unusual is driving channel A. **")
    print("   Check probe connections and signal source.")
else:
    enum, full_scale_v, label, voltage_mv = good_range
    dc_mv  = voltage_mv.mean()
    ac_mv  = voltage_mv.std()
    print(f"Signal fits within {label} (DC coupling).")
    print(f"  DC offset : {dc_mv:+.3f} mV")
    print(f"  AC noise  : {ac_mv:.3f} mV RMS  (≈{ac_mv*3:.3f} mV 3-sigma)")
    print()

    if abs(dc_mv) > full_scale_v * 500 * 0.1:
        print("RECOMMENDATION: The DC offset is large relative to the AC signal.")
        print("  → Use AC coupling (removes DC, reveals AC component at ±20 mV range).")
        print(f"  → Or use DC coupling with the {label} range set in the ratemeter UI.")
    else:
        print(f"RECOMMENDATION: DC coupling at {label} should work fine.")
        print("  Set the voltage range to this value in the ratemeter page Range spin box.")

# ── Close ──────────────────────────────────────────────────────────────────
ps.ps4000Stop(handle)
ps.ps4000CloseUnit(handle)
print("\nDone. Device closed.")
