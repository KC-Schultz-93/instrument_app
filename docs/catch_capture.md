# Catch capture (Ratemeter page)

Triggers on a signal, analyses the A+B waveform, **keeps** it (raw CSV) if it shows a caught
particle and **discards** it otherwise, counting both so the catch rate is visible.

Code: `services/catch_classifier.py` (decision), `services/catch_recorder.py` (files),
`services/catch_worker.py` (threads), `PicoScopeService.run_block_dual` (two-channel capture),
UI in `pages/ratemeter_page.py` (Catch tile, bottom-strip dashboard).

## Using it

1. Connect, then open the **Catch** tile (top bar) and tick **Catch capture mode**.
2. Scope: window long enough to contain the mirror switch *and* a few ms of particle signal
   (the first capture used 10 ms at 200 ns = 50 kS). Trigger: **enabled**, on channel A.
3. Channel panel sets channel A (forced to A in this mode). The Catch panel sets channel B
   (probe, range, coupling, bandwidth limit) and the classifier thresholds.
4. Type the mirror voltages into the box in the bottom strip. They can be edited while running;
   each saved file gets the text that was there when it was saved.
5. Press Run. Counts update live. A caught waveform is held on screen for 3 s.

Mode cannot be changed while running. The Ratemeter's band recorders are disabled in this mode.

## What counts as a catch

Judged only on the part of the trace after the mirror-switching transient
(`Analysis starts at`, default 3.5 ms from the first sample; the first capture had the switch at
~2.2 ms and the settling tail gone by ~3.3 ms). Channels are high-passed to remove the slow
baseline recovery, then all of these must hold:

| Gate | Meaning | Default | First capture measured |
|---|---|---|---|
| A repetition | autocorrelation peak of A within the allowed period range | ≥ 0.40 | 0.78 |
| B repetition | B's autocorrelation at A's period (±15 %) | ≥ 0.30 | 0.49 |
| Signal vs noise | rms of the folded (cycle-averaged) waveform / cycle-to-cycle residual, both channels | ≥ 1.0 | A 1.8, B 1.3 |
| A bipolar | folded A has a significant positive **and** negative lobe, comparable size | balance ≥ 0.30 | 0.78 |
| B square-like | harmonic content (2nd–5th / fundamental) of folded B; pure sine ≈ 0, ideal square ≈ 0.39 | ≥ 0.10 (0 = off) | 0.71 |

**There is no fixed frequency window** — the frequency carries the kinetic energy. The only
limits on the period are that the pattern repeats at least `Minimum repeats in window` times
(default 5, so the slowest detectable period is analysis length / 5) and is longer than
`Shortest period considered` (default 20 µs). The Catch panel shows the resulting range.

Every result records its measurements (period, frequency, coherences, SNR, lobe counts, B shape,
A≈dB/dt correlation) in the saved `.json`, so thresholds can be tuned from real waveforms.

## What is saved

```
Recorded Data/Catch/<session>/          one folder per Run (restarts mid-run reuse it)
    catch_0001_<timestamp>.csv          raw A+B waveform, PicoScope-export layout (ms, mV, mV)
    catch_0001_<timestamp>.json         mirror voltages, measured features, all settings
    index.csv                           one row per saved waveform, with mirror voltages
    session.json                        final counts, settings, mirror-voltage history
```

* Channel B is saved as the scope input in true (probe-scaled) mV. Converting to charge is
  downstream analysis. (The first example file was exported by PicoScope 7 already in electrons and
  was 50× too large because of the probe setting; that does not affect the classifier, which is
  relative to each channel's own noise.)
* If the classifier itself ever raises, the trace is **kept** as `unclassified_*` and counted under
  "errors" rather than lost.
* Files are written via a temporary name and renamed, so a crash never leaves a partial waveform.

## Counts

| Count | Meaning |
|---|---|
| Triggers | blocks where A actually reached the trigger level |
| Catches / Non-catches | triggers that were analysed |
| Catch rate | catches / (catches + non-catches), plus catches per minute |
| auto-trigger timeouts | blocks returned only because the scope's auto-trigger timer expired. **Not** counted as triggers |
| dropped | triggers lost because analysis fell behind and the queue (8 traces) was full |
| errors | classifier or save failures |

Set the Trigger panel's *Auto-timeout* to 0 to wait indefinitely for real triggers; Stop stays
responsive because the wait is abortable.

## Threads

Acquisition thread: arm → wait for trigger → read A+B → queue → re-arm. Analysis thread: classify →
save or discard → update counts. Analysis can never delay re-arming; traces still queued when Stop
is pressed are analysed before the session summary is written.

## Replaying a file offline

```python
from instrument_app.services.catch_recorder import load_pico_csv       # also reads .csv.gz
from instrument_app.services.catch_classifier import classify_catch
t_ms, a, b = load_pico_csv("some_capture.csv")
print(classify_catch(a, b, (t_ms[1] - t_ms[0]) * 1e6))
```

`tests/pytest/data/catch_example_FIGURE_1.csv.gz` is the first caught particle and is the regression fixture.

## Known limits (read before trusting the counts)

* **Thresholds were tuned from one real catch plus synthetic non-catches.** Synthetic tests reject
  noise with settling tails, a single pass-through pulse, one-channel-only periodicity, 60 Hz pickup
  and a sinusoidal tone on both channels. Real non-catch captures should be used to re-tune. Missing a
  real catch is far costlier than keeping a false one, so the defaults are lenient.
* The two-channel scope path (`run_block_dual`) is tested against a fake driver only; its first
  run on the real PicoScope 4262 is the true test.
* One trigger per block (no rapid-block batching) in this mode; "Captures per batch" is ignored.
  If scope dead time proves limiting, dual-channel rapid block is the next step.
* The analysis window start is a fixed time after the first sample. If the mirror-switch delay
  changes, change `Analysis starts at`.
* Pure tones that also have harmonic content on both channels at the same frequency would pass.
