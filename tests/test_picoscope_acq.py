"""
tests/test_picoscope_acq.py
---------------------------
Minimal standalone acquisition test for PicoScope 4262.
Run directly with:  python tests/test_picoscope_acq.py

Prints step-by-step progress so you can see exactly where the
hardware pipeline stalls or produces wrong data, independently of the GUI.
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
ADC_MAX       = 32512
CHANNEL_A     = 0
RANGE_20MV    = 1        # PS4000_20MV  → ±20 mV
COUPLING_DC   = 1
TIMEBASE      = 23       # (23-2)*8 + 32 = 200 ns/sample
NUM_SAMPLES   = 5_000    # 5000 × 200 ns = 1 ms window (small for quick test)
AUTOTRIG_MS   = 2000     # 2-second safety auto-trigger

handle = ctypes.c_int16(0)

def check(status, label):
    code = status.value if hasattr(status, "value") else int(status)
    print(f"  {label}: status = {code:#010x} {'OK' if code == 0 else 'FAILED'}")
    if code != 0:
        sys.exit(f"Fatal SDK error in {label!r}: {code:#010x}")

# ── Open ───────────────────────────────────────────────────────────────────
print("Opening PicoScope 4262...")
check(ps.ps4000OpenUnit(ctypes.byref(handle)), "ps4000OpenUnit")
print(f"  Handle = {handle.value}")

# ── Channel setup ──────────────────────────────────────────────────────────
print("Configuring channels...")
for ch in range(2):
    enabled = 1 if ch == CHANNEL_A else 0
    rng     = RANGE_20MV if ch == CHANNEL_A else 7
    check(ps.ps4000SetChannel(handle, ch, enabled, COUPLING_DC, rng),
          f"ps4000SetChannel(ch={ch})")

# ── Trigger (disabled, with 2-second auto-trigger safety) ─────────────────
print("Disabling trigger (autoTrigger=2000 ms)...")
check(ps.ps4000SetSimpleTrigger(handle, 0, 0, 0, 2, 0, AUTOTRIG_MS),
      "ps4000SetSimpleTrigger(disabled)")

# ── Stop any leftover acquisition ─────────────────────────────────────────
ps.ps4000Stop(handle)

# ── RunBlock ───────────────────────────────────────────────────────────────
print(f"Starting block acquisition ({NUM_SAMPLES} samples @ timebase {TIMEBASE})...")
t_start = time.monotonic()
check(ps.ps4000RunBlock(handle, 0, NUM_SAMPLES, TIMEBASE, 1,
                        None, 0, None, None),
      "ps4000RunBlock")

# ── Poll IsReady ───────────────────────────────────────────────────────────
print("Polling ps4000IsReady...")
ready = ctypes.c_int16(0)
for i in range(15_000):            # 15-second timeout
    ps.ps4000IsReady(handle, ctypes.byref(ready))
    if ready.value:
        elapsed = time.monotonic() - t_start
        print(f"  Ready after {i+1} poll(s) / {elapsed*1000:.1f} ms")
        break
    if i % 500 == 0 and i > 0:
        print(f"  ... still waiting at {i} ms")
    time.sleep(0.001)
else:
    sys.exit("TIMEOUT: ps4000IsReady never returned 1 after 15 seconds.\n"
             "The device is not completing the acquisition.\n"
             "Try: close any other programs using the scope, reconnect USB.")

# ── Buffer + GetValues ─────────────────────────────────────────────────────
print("Registering data buffers...")
buf     = (ctypes.c_int16 * NUM_SAMPLES)()
buf_min = (ctypes.c_int16 * NUM_SAMPLES)()
check(ps.ps4000SetDataBuffers(handle, CHANNEL_A,
                              ctypes.byref(buf), ctypes.byref(buf_min),
                              NUM_SAMPLES),
      "ps4000SetDataBuffers")

print("Fetching values...")
overflow  = ctypes.c_int16(0)
n_values  = ctypes.c_int32(NUM_SAMPLES)
check(ps.ps4000GetValues(handle, 0, ctypes.byref(n_values), 1, 0, 0,
                         ctypes.byref(overflow)),
      "ps4000GetValues")

print(f"  Samples returned : {n_values.value}")
print(f"  Overflow flag    : {bool(overflow.value)}")

# ── Signal stats ───────────────────────────────────────────────────────────
raw     = np.frombuffer(buf, dtype=np.int16, count=n_values.value).astype(float)
voltage = raw / ADC_MAX * 0.02   # ±20 mV range

print(f"\nSignal statistics (channel A, ±20 mV range, {TIMEBASE*8-16}-ns samples):")
print(f"  ADC raw   min={raw.min():.0f}  max={raw.max():.0f}  std={raw.std():.1f}")
print(f"  Voltage   min={voltage.min()*1000:.2f} mV  "
      f"max={voltage.max()*1000:.2f} mV  "
      f"std={voltage.std()*1000:.3f} mV")

if raw.std() < 5:
    print("\n** WARNING: signal appears to be all-zeros or flat. **")
    print("   Check that channel A is actually connected to the signal.")
else:
    print("\nSignal looks non-zero — acquisition pipeline is working.")

# ── Close ──────────────────────────────────────────────────────────────────
ps.ps4000Stop(handle)
ps.ps4000CloseUnit(handle)
print("\nDone. Device closed.")
