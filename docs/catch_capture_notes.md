# Catch capture (Ratemeter) — status and decisions

Branch `claude/catch-capture` on KC-Schultz-93/instrument_app (not yet merged). Full user doc: `docs/catch_capture.md` in the repo.

## What it does
Trigger on A -> record A+B -> classify on a separate analysis thread -> keep catches as raw CSV (+ .json with mirror voltages, features, settings; per-session index.csv, session.json) -> discard the rest. Live counts: triggers, catches, non-catches, catch rate and /min, plus dropped / auto-trigger timeouts / errors.

## Definition of a catch (from the first capture, `FIGURE 1.csv`)
After the mirror switch (~2.2 ms) settles (analysis starts 3.5 ms): repeating bipolar lobes on A, repeating square wave on B at the same period, repeating signal above cycle-to-cycle residual. No fixed frequency window (frequency = kinetic energy); only "at least N repeats in window" (default 5) and a shortest period (20 us). First capture: 4.136 kHz, A coherence 0.78, B 0.49, B harmonic ratio 0.71.

## Decisions worth remembering
- Judge by folding at the found period, not by a high-frequency noise estimate: A's noise is strongly low-pass (hf sigma was 7.5x below its real noise).
- Missing a catch costs far more than a false keep, so uncertain gates are lenient; a classifier crash keeps the trace as `unclassified_*`.
- B square-wave gate on by default at 0.10 (real 0.71, sine ~0). A~dB/dt correlation is recorded but NOT gated (a sine scores high too).
- Counts: auto-trigger timeouts (A never reached the trigger level) are not counted as triggers. Queue-full traces are counted as dropped.
- UI: Catch tile in the top bar; bottom strip swaps to a catch dashboard. Rail untouched (page min height stays 535 px; an earlier rail-based layout reached 849).
- Test fixture is gzip (`tests/pytest/data/catch_example_FIGURE_1.csv.gz`, 457 KB) because tests/ is synced into this project's knowledge.
- Fixed unrelated: Rates panel's unwrapped "Electrode length" label forced the dock to 356 px and clipped every dock panel.

## Open items
- `run_block_dual` / `configure_channels_dual` tested only against a fake driver; first run on the real 4262 is the real test (A+B enabled together, SetDataBuffers for both channels).
- Thresholds tuned from ONE real catch + synthetic non-catches. Re-tune once real non-catch waveforms exist (each saved .json holds all features).
- One trigger per block; dual-channel rapid block is the next step if scope dead time matters.
- Channel B saved as true mV (probe-scaled). The example file's B was in electrons and 50x too large (probe setting at recording time).
- 4 pre-existing failures in tests/pytest/test_event_detector.py (empty trace, periodic peaks, spacing) — fail identically on untouched main.
- Window needs >= ~6 ms with default settings (4.5 ms was too short for the first capture; 5 ms passes with 6 cycles).