"""
bench_mirror_hold_feasibility.py
--------------------------------
STEP 0 of docs/Pulser_Triggering.md: standalone hardware experiments that decide
whether the "AWG starts on scope trigger, software releases it" mirror hold can
work on the PicoScope 4262. This is a bench script, NOT app code, and it is not
collected by pytest.

!! SAFETY !!
  * The AWG output goes to a +1 V DC level. DISCONNECT it from the pulser / HV
    hardware first. Wire it ONLY to the scope:  AWG out --T--> Channel B input.
  * Channel A needs a way to produce a trigger edge (see --trig-v): a function
    generator, or a quick finger-tap on the A probe tip for a noise spike.
  * The script always turns the AWG off and stops the scope on exit.

Experiments (python tests/bench_mirror_hold_feasibility.py <name> [options])
  constants   (no hardware) Compare the local SDK enum values with ps4000Api.h.
  dc          +1 V DC straight from the AWG; read it on B; compare with SigGenOff
              and with "set 0 V" as ways to turn the output off (idle level).
  oneshot     AWG armed on the SCOPE trigger while the scope STREAMS A and B.
              You apply an edge to A; we report whether B rose, the trigger->edge
              latency (200 ns resolution), the hold length, the level after the
              shot ends, and whether a second A edge re-fires it.
  soft        AWG armed on SOFT_TRIG, fired by ps4000SigGenSoftwareControl, then
              released with a host command: approximate host->output latency.
  all         constants, dc, oneshot, soft in order.

Report each result back; if `oneshot` does not start the output while streaming,
the fallback is block mode (see the md, Step 0).
"""
from __future__ import annotations

import argparse
import ctypes
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np

HEADER = Path(r"C:\Program Files\Pico Technology\SDK\inc\ps4000Api.h")

# ---------------------------------------------------------------------------
# Local copies of the SDK constants. `constants` checks these against the header;
# copy them into services/picoscope_service.py only after that passes.
# ---------------------------------------------------------------------------
LOCAL_CONSTANTS = {
    "WAVE_TYPE": {"PS4000_DC_VOLTAGE": 8},
    "SIGGEN_TRIG_TYPE": {"SIGGEN_RISING": 0, "SIGGEN_FALLING": 1,
                         "SIGGEN_GATE_HIGH": 2, "SIGGEN_GATE_LOW": 3},
    "SIGGEN_TRIG_SOURCE": {"SIGGEN_NONE": 0, "SIGGEN_SCOPE_TRIG": 1, "SIGGEN_AUX_IN": 2,
                           "SIGGEN_EXT_IN": 3, "SIGGEN_SOFT_TRIG": 4},
    "SWEEP_TYPE": {"UP": 0},
    "PS4000_TIME_UNITS": {"PS4000_NS": 2, "PS4000_US": 3},
}
WAVE_DC = 8
TRIG_RISING = 0
SRC_NONE, SRC_SCOPE, SRC_SOFT = 0, 1, 4
SWEEP_UP = 0
TIME_NS = 2
CH_A, CH_B = 0, 1


def banner(text: str) -> None:
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72)


def result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------------------
# constants (offline)
# ---------------------------------------------------------------------------

def parse_enums(text: str) -> dict:
    """{typedef name: {member: value}} for every `typedef enum ... {...} NAME;`."""
    out = {}
    for m in re.finditer(r"typedef\s+enum\s*\w*\s*\{(.*?)\}\s*(\w+)\s*;", text, re.S):
        body, name = m.group(1), m.group(2)
        members, nxt = {}, 0
        for item in body.split(","):
            item = re.sub(r"//.*", "", item).strip()
            if not item:
                continue
            if "=" in item:
                k, v = (s.strip() for s in item.split("=", 1))
                nxt = members[v] if v in members else int(v, 0)
            else:
                k = item
            members[k] = nxt
            nxt += 1
        out[name] = members
    return out


def exp_constants() -> bool:
    banner("constants: local SDK enum values vs ps4000Api.h")
    if not HEADER.exists():
        return result("header present", False, str(HEADER))
    text = HEADER.read_text(errors="replace")
    enums = parse_enums(text)
    ok = True
    for enum_name, wanted in LOCAL_CONSTANTS.items():
        have = enums.get(enum_name)
        if have is None:
            ok &= result(f"enum {enum_name}", False, "not found in header")
            continue
        for member, value in wanted.items():
            full = member if member in have else next((k for k in have if k.endswith(member)), None)
            good = full is not None and have[full] == value
            ok &= result(f"{enum_name}.{member}", good,
                         f"local={value} header={have.get(full)}")
    for macro in ("PS4262_MAX_VALUE", "MAX_SIG_GEN_FREQ_4262", "MIN_SIG_GEN_FREQ",
                  "PS4262_MAX_WAVEFORM_BUFFER_SIZE", "PS4262_MIN_DWELL_COUNT"):
        m = re.search(rf"#define\s+{macro}\s+(\S+)", text)
        print(f"   {macro} = {m.group(1) if m else '?'}")
    try:
        from instrument_app.services.picoscope_service import _ADC_MAX
        print(f"   service _ADC_MAX = {_ADC_MAX} (header PS4262_MAX_VALUE above; "
              "if they differ, ADC->volts is off by their ratio - report it)")
    except Exception as exc:                                  # pragma: no cover
        print(f"   (could not import service: {exc})")
    print("   AWG max output voltage is NOT in the header: read it from the 4262 "
          "datasheet / confirm with the dc experiment.")
    return ok


# ---------------------------------------------------------------------------
# hardware rig
# ---------------------------------------------------------------------------

class Rig:
    def __init__(self, range_a_v: float, range_b_v: float, interval_ns: int = 200) -> None:
        from instrument_app.services.picoscope_service import PicoScopeService, _ADC_MAX
        self.svc = PicoScopeService()
        self.svc.connect()
        self.ps = self.svc._get_ps()
        self.h = self.svc._handle
        self.adc_max = _ADC_MAX
        self.interval_ns = interval_ns
        self.range_v = {CH_A: range_a_v, CH_B: range_b_v}
        for ch in (CH_A, CH_B):
            st = self.ps.ps4000SetChannel(self.h, ch, 1, 1,          # enabled, DC coupling
                                          self.svc._nearest_range_enum(self.range_v[ch]))
            self.svc._check_status(st, f"SetChannel({ch})")
        self.dt_us = interval_ns / 1000.0

    def volts(self, raw: np.ndarray, ch: int) -> np.ndarray:
        return raw.astype(np.float64) / self.adc_max * self.range_v[ch]

    def check(self, st, op):
        self.svc._check_status(st, op)

    # --- AWG ---------------------------------------------------------------
    def awg_dc(self, level_v: float, trig_source: int, duration_s: float = 0.0,
               shots: int = 0) -> None:
        """DC level. shots=0 -> free-running; shots=1 with duration_s -> one-shot
        (the built-in generator times a 'cycle' as 1/frequency)."""
        freq = (1.0 / duration_s) if duration_s > 0 else 1.0
        self.check(self.ps.ps4000SetSigGenBuiltIn(
            self.h, int(round(level_v * 1e6)), 0, WAVE_DC,
            freq, freq, 0.0, 0.0, SWEEP_UP, 0, shots, 0,
            TRIG_RISING, trig_source, 0), "SetSigGenBuiltIn")

    def awg_off(self) -> None:
        try:
            self.ps.ps4000SigGenOff(self.h)
        except Exception as exc:                              # pragma: no cover
            print(f"   (SigGenOff failed: {exc})")

    def awg_zero(self) -> None:
        self.check(self.ps.ps4000SetSigGenBuiltIn(
            self.h, 0, 0, WAVE_DC, 1.0, 1.0, 0.0, 0.0, SWEEP_UP, 0, 0, 0,
            TRIG_RISING, SRC_NONE, 0), "SetSigGenBuiltIn(0 V)")

    # --- scope trigger -----------------------------------------------------
    def set_trigger(self, enabled: bool, threshold_v: float = 0.0, auto_ms: int = 0) -> None:
        thr = int(threshold_v / self.range_v[CH_A] * self.adc_max)
        self.check(self.ps.ps4000SetSimpleTrigger(
            self.h, 1 if enabled else 0, CH_A, thr, 2, 0, auto_ms), "SetSimpleTrigger")

    # --- streaming ---------------------------------------------------------
    def stream(self, seconds: float, pre_samples: int = 5000, do_at=None, do_at_s: float = 0.0):
        """Stream A and B for `seconds`. `do_at` (callable) runs once at do_at_s.
        Returns dict(a, b, trigger_idx, t_do_idx) with raw int16 arrays."""
        ps, h = self.ps, self.h
        n_buf = 1 << 21
        bufs = {}
        for ch in (CH_A, CH_B):
            mx, mn = (ctypes.c_int16 * n_buf)(), (ctypes.c_int16 * n_buf)()
            # plural form, as in picoscope_service.run_block (driver quirk)
            self.check(ps.ps4000SetDataBuffers(h, ch, ctypes.byref(mx), ctypes.byref(mn), n_buf),
                       f"SetDataBuffers({ch})")
            bufs[ch] = (mx, np.ctypeslib.as_array(mx))
        got = {CH_A: [], CH_B: []}
        state = {"total": 0, "trigger": None, "overflow": 0}

        def on_ready(handle, n, start, overflow, trig_at, triggered, auto_stop, param):
            if n <= 0:
                return
            for ch in (CH_A, CH_B):
                got[ch].append(bufs[ch][1][start:start + n].copy())
            if triggered and state["trigger"] is None:
                state["trigger"] = state["total"] + (trig_at - start)   # VERIFY semantics
            state["overflow"] |= int(overflow)
            state["total"] += n

        cfunc = ps.StreamingReadyType(on_ready)
        interval = ctypes.c_uint32(self.interval_ns)
        max_post = int(seconds * 1e9 / self.interval_ns) + pre_samples
        self.check(ps.ps4000RunStreaming(h, ctypes.byref(interval), TIME_NS, pre_samples,
                                         max_post, 0, 1, n_buf), "RunStreaming")
        print(f"   streaming at {interval.value} ns/sample")
        t0 = time.perf_counter()
        done_action, action_idx = False, None
        try:
            while time.perf_counter() - t0 < seconds:
                self.check(ps.ps4000GetStreamingLatestValues(h, cfunc, None), "GetStreamingLatestValues")
                if do_at and not done_action and time.perf_counter() - t0 >= do_at_s:
                    action_idx = state["total"]            # samples collected so far (approx)
                    do_at()
                    done_action = True
                time.sleep(0.0005)
        finally:
            ps.ps4000Stop(h)
        a = np.concatenate(got[CH_A]) if got[CH_A] else np.zeros(0, np.int16)
        b = np.concatenate(got[CH_B]) if got[CH_B] else np.zeros(0, np.int16)
        return {"a": a, "b": b, "trigger_idx": state["trigger"], "action_idx": action_idx,
                "overflow": state["overflow"]}

    def close(self) -> None:
        self.awg_off()
        try:
            self.ps.ps4000Stop(self.h)
        except Exception:
            pass
        self.svc.disconnect()


def first_cross(x: np.ndarray, level: float, rising=True, start=0):
    idx = np.flatnonzero((x[start:] >= level) if rising else (x[start:] <= level))
    return None if idx.size == 0 else int(idx[0] + start)


# ---------------------------------------------------------------------------
# experiments
# ---------------------------------------------------------------------------

def exp_dc(rig: Rig, level: float) -> bool:
    banner(f"dc: +{level} V DC from the AWG, read on channel B")
    ok = True
    rig.set_trigger(False)
    rig.awg_dc(level, SRC_NONE)
    time.sleep(0.05)
    r = rig.stream(0.05)
    b = rig.volts(r["b"], CH_B)
    mean = float(b[len(b) // 4:].mean())
    ok &= result("DC level reaches B", abs(mean - level) < 0.05 * max(level, 0.1),
                 f"measured {mean:.4f} V for {level} V requested (ADC_MAX scale; "
                 f"if off by ~{32512/32767:.4f} see _ADC_MAX note)")

    for label, off in (("SigGenOff", rig.awg_off), ("set 0 V", rig.awg_zero)):
        rig.awg_dc(level, SRC_NONE)
        time.sleep(0.05)
        r = rig.stream(0.1, do_at=off, do_at_s=0.04)
        b = rig.volts(r["b"], CH_B)
        idx = first_cross(b, 0.5 * level, rising=False,
                          start=int((r["action_idx"] or 0)))
        tail = float(b[-len(b) // 10:].mean())
        ok &= result(f"output after {label}", abs(tail) < 0.05 * max(level, 0.1),
                     f"tail {tail:.4f} V; fell {'at sample %d' % idx if idx else 'NOT seen'}"
                     f" (0 V = off is required; a held level means this method can't release)")
    rig.awg_off()
    return ok


def exp_oneshot(rig: Rig, level: float, hold_s: float, trig_v: float, wait_s: float) -> bool:
    banner("oneshot: AWG armed on the SCOPE trigger while streaming A and B")
    print(f"   Apply a rising edge above {trig_v} V to channel A, once, within {wait_s:.0f} s.")
    print("   Optionally apply a SECOND edge ~0.3 s later to see if the shot re-fires.")
    rig.awg_off()
    rig.set_trigger(True, trig_v, auto_ms=0)
    rig.awg_dc(level, SRC_SCOPE, duration_s=hold_s, shots=1)
    r = rig.stream(wait_s, pre_samples=5000)
    a, b = rig.volts(r["a"], CH_A), rig.volts(r["b"], CH_B)
    print(f"   collected {len(a)} samples; driver trigger index = {r['trigger_idx']}; "
          f"overflow flags = {r['overflow']}")
    ia = first_cross(a, trig_v)
    ib = first_cross(b, 0.5 * level)
    ok = True
    ok &= result("A edge seen", ia is not None, f"sample {ia}")
    ok &= result("AWG started from the scope trigger (streaming mode)", ib is not None,
                 f"B rose at sample {ib}")
    if ia is not None and ib is not None:
        lat_us = (ib - ia) * rig.dt_us
        print(f"   trigger->output latency = {lat_us:.1f} us "
              f"(resolution {rig.dt_us:.1f} us; the mirror needs <~59 us)")
        high = b > 0.5 * level
        end = ib
        while end < len(high) and high[end]:
            end += 1
        held_ms = (end - ib) * rig.dt_us / 1000.0
        ended = end < len(high)
        print(f"   output stayed high {held_ms:.1f} ms "
              f"({'ended inside the record' if ended else 'still high when the record ended'}; "
              f"requested {hold_s * 1000:.0f} ms)")
        if ended:
            tail = float(b[-max(1, len(b) // 20):].mean())
            result("idle level after the shot ends", abs(tail) < 0.05 * max(level, 0.1),
                   f"{tail:.4f} V (must be 0 V)")
        rises = np.flatnonzero(np.diff((b > 0.5 * level).astype(np.int8)) == 1)
        edges = np.flatnonzero(np.diff((a > trig_v).astype(np.int8)) == 1)
        print(f"   A rising edges: {len(edges)}   B rising edges: {len(rises)} "
              "(B edges > 1 means the one-shot re-fired without reconfiguring)")
    rig.awg_off()
    return ok


def exp_soft(rig: Rig, level: float) -> bool:
    banner("soft: SOFT_TRIG start + host-command release (approximate latency)")
    rig.set_trigger(False)
    rig.awg_dc(level, SRC_SOFT, duration_s=5.0, shots=1)
    fired = {}

    def fire():
        fired["t"] = time.perf_counter()
        rig.check(rig.ps.ps4000SigGenSoftwareControl(rig.h, 1), "SigGenSoftwareControl")

    r = rig.stream(0.15, pre_samples=0, do_at=fire, do_at_s=0.03)
    b = rig.volts(r["b"], CH_B)
    ib = first_cross(b, 0.5 * level)
    ok = result("SOFT_TRIG starts the output", ib is not None,
                f"B rose at sample {ib}, action issued near sample {r['action_idx']}")
    if ib is not None and r["action_idx"] is not None:
        print(f"   host command -> output ~ {(ib - r['action_idx']) * rig.dt_us / 1000:.2f} ms "
              "(approximate: includes streaming poll granularity)")
    rig.awg_off()
    return ok


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("experiment", choices=["constants", "dc", "oneshot", "soft", "all"])
    ap.add_argument("--level", type=float, default=1.0, help="AWG DC level in volts (default 1.0)")
    ap.add_argument("--hold-s", type=float, default=0.2, help="one-shot length in s (default 0.2)")
    ap.add_argument("--trig-v", type=float, default=0.1, help="rising trigger level on A, volts")
    ap.add_argument("--wait-s", type=float, default=15.0, help="time to apply the A edge in `oneshot`")
    ap.add_argument("--range-a", type=float, default=0.5, help="channel A range, volts")
    ap.add_argument("--range-b", type=float, default=2.0, help="channel B range, volts")
    ap.add_argument("--yes", action="store_true", help="skip the wiring confirmation")
    args = ap.parse_args()

    results = []
    if args.experiment in ("constants", "all"):
        results.append(exp_constants())
    if args.experiment == "constants":
        return 0 if results[0] else 1

    banner("WIRING CHECK")
    print("AWG output must go ONLY to scope channel B - not to the pulser / HV hardware.")
    if not args.yes and input("Type 'yes' once the AWG is disconnected from the pulser: ").strip().lower() != "yes":
        print("Aborted.")
        return 2

    rig = Rig(args.range_a, args.range_b)
    try:
        rig.awg_off()
        if args.experiment in ("dc", "all"):
            results.append(exp_dc(rig, args.level))
        if args.experiment in ("oneshot", "all"):
            results.append(exp_oneshot(rig, args.level, args.hold_s, args.trig_v, args.wait_s))
        if args.experiment in ("soft", "all"):
            results.append(exp_soft(rig, args.level))
    finally:
        rig.close()
        print("\nAWG off, scope closed.")
    banner("SUMMARY")
    print("All experiments passed." if all(results) else
          "At least one experiment FAILED - report it before building the app code.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
