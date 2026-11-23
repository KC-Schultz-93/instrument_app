Mirror hold via PicoScope 4262 AWG (software-released) - implementation plan

Status (2026-11-23): Phase 1 implemented - the offline detector and the Step 0 bench script. Steps 1, 3, 4, 5 (service, controller, UI, safety) are NOT started; they wait for the Step 0 hardware results. Hardware/API facts marked VERIFY are not confirmed. Do not guess enum values; read them from ps4000Api.h.

Done so far
- services/hold_detector.py - pure detector (Step 2). Tests: tests/pytest/test_hold_detector.py (Step 6, offline parts), including the real FIGURE 3 capture (tests/pytest/data/catch_example_FIGURE_3.csv.gz). On FIGURE 3 it confirms at ~5 ms and never releases (199 hits, 0 missed passes). A loss is reported ~1.5 ms after the last pass (max_missed=5 periods + filter margin).
- tests/bench_mirror_hold_feasibility.py - Step 0 experiments (constants / dc / oneshot / soft). Only `constants` has been run (all enum values match ps4000Api.h). The hardware experiments are written but NOT yet run on a scope.
- services/catch_classifier.py: helpers renamed public (fill_nonfinite, highpass, autocorr_unbiased, first_strong_peak) so the detector reuses them.

Findings that differ from the plan below
- There is no existing handle lock in picoscope_service.py (it has no threading at all); Step 1 must add one.
- Test fixtures live in tests/pytest/data/, not tests/data/.
- The app creates two PicoScopeService instances (DAQ page, Ratemeter page); the controller must share the one physical scope and emit daq_busy.
- The pulser is external hardware; nothing in this repo drives it.
- A's noise floor is low-frequency wobble, not white noise, so the detector does not use a k_sigma threshold. A pass is an envelope peak above peak_frac x P90 of the envelope while confirming, then hold_frac x the median height of recent hits.
- Header facts: SIGGEN_TRIG_SOURCE SCOPE_TRIG=1, SOFT_TRIG=4; SIGGEN_TRIG_TYPE RISING=0; WAVE_TYPE DC_VOLTAGE=8; offset/pkToPk are in microvolts; PS4262_MAX_VALUE is 32767 but picoscope_service uses _ADC_MAX=32512 (check against the dc experiment; do not change silently).
- docs/trajectory_fit_model.md and docs/echo_validated_runt_trigger_plan.md are referenced below but do not exist in the repo.

Goal

Keep the front-mirror HV pulser ON (AWG output +1 V) from the moment a particle is trapped until the particle's signal is lost, then drop the output to 0 V and wait for the next particle.

The current pulser turns the mirror ON when A crosses a set-point and leaves it on for a fixed time. That cannot extend the hold, and the edge trigger can miss weak particles.

Design in one paragraph

Hardware starts it, software stops it.

The AWG is armed with trigger source = scope trigger and outputs a DC +1 V one-shot of long, fixed duration (seconds). The mirror switches within microseconds of the trigger, because the mirror must switch about 59 us after the trigger (see trajectory_fit_model.md) and host latency (ms) is too slow.
The scope streams A and B to the host. A detector watches A for periodic passes. When the passes stop, the host sets the AWG output to 0 V. The long one-shot is the failsafe: if the app hangs, the output ends by itself.
Evidence from FIGURE_3.csv (5 MS/s, 50 ms, one captured particle)
Item	Value
A overrange	0.060-0.073 ms (68 samples)
B railed	0.061-2.64 ms
A transient	large (about 109 mV p-p) until ~3.5 ms
A after 3.5 ms	~19 mV p-p, 3.8 mV rms, no decay through 50 ms
Pass spacing	median 231 us; autocorr period drifts 235 -> 226 us over 50 ms
Missed passes	27 of ~170 gaps are ~2x median (single dropped pass)
Noise burst	11.79 ms, -66 mV, one event

Consequences for the design:

The particle never left in this file. It cannot tune the loss threshold. Capture a longer record that contains a real loss (see Step 7).
Single missed passes are normal. Loss must mean several consecutive missed passes.
Amplitude alone fails (noise burst). Use periodicity: expected pass times predicted from the tracked period.
The period drifts about 4% in 50 ms, so the detector must track it.
Blank the first ~3.5 ms after the trigger (switch transient).
State machine
DISARMED -> ARMED -> BLANKING -> CONFIRMING -> HOLDING -> RELEASING -> ARMED
                         \            \------------------^ (no catch by deadline)
ARMED: scope streaming, trigger armed, AWG armed (scope-trigger source), output 0 V.
BLANKING (0 to blank_ms, default 3.5): AWG is on; ignore A.
CONFIRMING (until confirm_deadline_ms, default 10 after blanking ends, so tune to the first-catch criteria; catch_capture needs >= ~6 ms and >= 5 repeats): need at least min_passes (default 5) with consistent spacing. If not met, release (false trigger).
HOLDING: track period, predict next pass, count consecutive missed predictions. Release when missed >= max_missed (default 5, about 1.2 ms) or at hold_max_s (hard cap).
RELEASING: set output to 0 V, log the event, re-arm the AWG and trigger, return to ARMED.
Step 0 - feasibility experiments (do first; each is small)

Write a standalone script, not app code. Stop and report if any fails.

VERIFY in ps4000Api.h: the signal-generator trigger-source enum has a scope-trigger value; the trigger-type enum (rising/falling/gate); ps4000SetSigGenBuiltIn, ps4000SetSigGenArbitrary, ps4000SigGenSoftwareControl, AWG buffer size and update-rate limits, and the 4262's maximum output voltage. Define local int constants in picoscope_service.py (the Python wrapper has no enums for some of these).
DC level: +1 V output with zero peak-to-peak and offset +1 V (built-in), or a constant arbitrary buffer. Confirm the level with a DMM or a scope channel.
One-shot armed on the scope trigger: does the output start on a scope trigger in streaming mode (ps4000RunStreaming with a trigger set)? VERIFY this. Also check what the streaming trigger does (autostop vs continue) and whether it re-arms.
After the shot ends, or after a stop command, does the output return to 0 V or hold the last value? The pulser needs 0 V = OFF. VERIFY.
Latency: trigger-to-output edge (loop the output into a scope channel) and host-command-to-output-change. Compare with the existing pulser.

Fallback if (3) fails: use block mode with a long window (about 50-100 ms, like FIGURE_3), AWG on scope trigger, and evaluate the block when it completes. Loss resolution is then the block length. Call the stop from another thread if the driver allows it during a block capture. Report this before choosing it.

Step 1 - PicoScopeService additions (services/picoscope_service.py)
arm_mirror_hold(level_v, max_s): configure the AWG one-shot on the scope trigger.
release_mirror(): set output to 0 V (idempotent, thread-safe via a handle lock that must be ADDED - none exists yet).
start_streaming(config) / read_stream_chunk() for A and B with a ring buffer. Channel B is saved probe-scaled (true mV), as in catch_capture.
Always return the output to 0 V on disconnect, close, exception and error.
Step 2 - detector (services/hold_detector.py, pure, offline-testable)

Input: chunks of A (mV) at a fixed sample rate; parameters in a dataclass.

High-pass / detrend A lightly. Estimate sigma robustly (MAD) from the blanking-free part.
Pass detection: find peaks of |A| above k_sigma (default 4) with min spacing about 100 us. Pair into a pass train. Do not rely on amplitude for loss.
Period tracking: after min_passes, estimate spacing (median of recent gaps), then predict each next pass. A prediction is hit if a pass lies within +/-20% of the period.
missed = consecutive predictions with no hit. Reset on a hit. Re-anchor on a hit.
Return per-chunk: state, period, hits, missed, reason (lost, no_catch, max_hold).
Single noise bursts: a burst that does not fit the predicted timing is not a hit.
Reuse catch_capture periodicity code where it fits; do not duplicate it.
Step 3 - controller (services/mirror_hold.py)
Owns the state machine, the stream reader thread and the detector.
Release decision runs per chunk (about 0.25-1 ms of data) on the reader thread.
Event log per hold: trigger time, duration, passes, final period, release reason; raw A and B for the hold saved as CSV plus JSON (same layout as catch_capture, same session folder). Keep a ring buffer so a short pre-trigger is saved too.
Counters: triggers, confirmed catches, false triggers (no_catch), mean hold time, release reasons.
Step 4 - UI
"Mirror hold" panel near the Catch tile: enable, level (V, default +1.0), max_missed, blank_ms, confirm_deadline_ms, hold_max_s, k_sigma; state label; live counters.
Settings in QSettings under mirror_hold/*.
Mutually exclusive with ratemeter/DAQ block capture (reuse daq_busy).
Disabled until a scope is connected. A manual "Force release" button.
Step 5 - safety
Hard cap hold_max_s (default a few seconds). The AWG one-shot duration is the hardware backstop, set a little above hold_max_s.
Exception or lost scope connection in the controller: attempt release, log it, never retry silently.
Do not change the pulser, HV supply or its setpoints from this code.
The output must be 0 V at startup and after every run.
Step 6 - tests
Offline with FIGURE_3.csv (gzipped copy in tests/pytest/data/catch_example_FIGURE_3.csv.gz; the file has infinity strings for overrange samples, which catch_recorder.load_pico_csv turns into NaN): the detector must NOT release between 3.5 ms and 50 ms, despite the single missed passes and the 11.79 ms burst.
Synthetic: truncate the signal at t0 -> release within about max_missed x period plus one chunk; noise-only after the trigger -> release at the confirm deadline; a burst without a periodic train -> no confirmation; period drift of 4% -> stays held.
State machine unit tests with a fake driver, like the catch_capture tests.
Hardware: loopback the AWG output to a scope channel and check on/off timing.
Step 7 - data to collect on hardware
A capture long enough to contain a real loss (hundreds of ms, or until the particle leaves) with the mirror held on. Use it to tune max_missed, k_sigma and confirm_deadline_ms.
Weak particles the edge trigger misses: needs a more sensitive scope trigger (runt trigger plan in echo_validated_runt_trigger_plan.md). Out of scope here.
Open items
Whether the 4262 scope-trigger path works in streaming mode (Step 0.3).
Idle output level after stop (Step 0.4).
Whether the existing pulser has its own on-time/threshold that must be set to "always on" or bypassed when the AWG drives it. Ask the user before wiring changes.
Does one trigger per hold waste particles arriving during the hold? Expected: ignored while holding.