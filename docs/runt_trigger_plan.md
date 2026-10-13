# Echo-validated runt trigger (pseudo-trigger) for reflected particle signals

## Goal

Record reflected-particle waveforms in rapid block, but only **keep** a segment when a
second matching pulse follows the first within a user-set time window. Hardware triggers
on the first pulse (runt trigger, adjustable like PicoScope 7); the "second signal" check
is a **post-capture software validator**, because rapid block cannot veto individual triggers.

## Evidence from `1900V_reflec_43.csv` (5 MS/s, 2 ms, 10004 samples)

- Channel A: bipolar pulse ~60 us peak-to-peak (+0.27 / -0.35 mV), noise ~16 uV rms (~17:1).
- A second near-identical pulse follows **~112 us** later. Normalized shape correlation
  ~0.99 (A) and ~1.0 (B).
- **Amplitude of the echo (corrected):** measured as step height from the local baseline,
  the two pulses in B are 1.02 and 0.97 mV (~5% apart); A positive peaks are 0.235 and
  0.233 mV; A negative lobes are -0.38 and -0.33 mV (~13% apart). An earlier note that the
  echo differed by 30-40% came from comparing absolute B maxima (1.38 vs 0.83 mV), which is
  misleading because B's baseline wanders by +/-0.5 mV. Still match primarily on normalized
  shape correlation; an amplitude-ratio gate of about +/-30% is reasonable as a starting point.
- Single file, one event: treat 112 us and the tolerances as starting points, not constants.

## Channel A vs channel B (trigger channel vs amplitude channel)

- A behaves like a **time derivative of B** (B is the less-filtered square-ish pulse train).
  Raw A-B correlation is low (~0.13) because of that, but A vs dB/dt correlates at ~0.96
  with A lagging by ~3.6 us.
- Empirical scale for this event (B step ~18 us 10-90 rise, flat top ~40 us): A peak is
  **~0.23 mV per 1 mV of B step height** (four edges: 0.215-0.25). So a 1 mV pulse in B gives
  about +0.23 mV (rising edge) and about -0.23 mV (falling edge, per 1 mV of fall) in A.
- Caveat: A is edge-speed dependent, so the 0.23 ratio holds for similar particle velocity
  (similar B edge rise time). Faster particles give a larger A for the same charge; slower
  ones give less. Calibrate over many captures (see `reflection_survey.py`) before using
  it to set trigger thresholds from charge.
- Derive charge from B step height from its local baseline, not from the absolute B level.

## Step 0 - runt trigger on the ps4000 driver (do first)

- Runt is present in `ps4000Api.h` (confirmed by user). The Python wrapper
  (`picosdk/ps4000.py`, PyPI 1.1 and GitHub master) defines **no** enums for threshold
  direction / threshold mode / pulse-width type; those parameters are raw ints.
  (`ps4000a.py` does define `PS4000A_POSITIVE_RUNT`/`NEGATIVE_RUNT`, which is a different
  driver.)
- Read the exact `THRESHOLD_DIRECTION`, `THRESHOLD_MODE` and `TRIGGER_CHANNEL_PROPERTIES`
  / `TRIGGER_CONDITIONS` definitions from the header and define local int constants for the
  runt values in `picoscope_service.py`. **Do not guess enum values.**
- Runt needs two thresholds (upper and lower, plus hysteresis) set through
  `ps4000SetTriggerChannelProperties`, with matching conditions/directions calls.
  `ps4000SetSimpleTrigger` only takes one threshold, so runt cannot go through it.
- Verify on hardware: a wrong direction value returns `PICO_INVALID_THRESHOLD_DIRECTION`
  (0x301, in `picosdk/constants.py`).
- Fallback if runt misbehaves: edge trigger low (~5 sigma, ~80-100 uV on A). The validator
  below rejects the extra false triggers.

## Existing code to build on (from other project docs)

- Rapid block already planned/implemented: `spin_captures_per_batch`,
  `ps4000MemorySegments`, `SetNoOfCaptures`, `SetDataBufferBulk`, `GetValuesBulk`
  (5 args). See `ps4000_bwfilter_rapidblock.md`.
- Trigger UI: `chk_trigger_enable`, `spin_trigger_threshold`, `combo_trigger_direction`,
  `_trigger_direction_value()`, `spin_trigger_auto`. Add "Runt (+)" / "Runt (-)" entries to
  the direction combo and a second threshold control (`spin_trigger_threshold_lower`)
  shown only for runt.
- Detection: `services/matched_filter.py` `detect()` with template and
  `correlation_threshold`, `min_distance_us`, `scale_factors`. Reuse it for the validator.

## Design

1. **Capture**: segment length = pre-trigger baseline + first pulse + `echo_max_us` + margin.
   Check against the `nMaxSamples` returned by `ps4000MemorySegments`; error out, do not
   truncate silently.
2. **Validate each segment after `GetValuesBulk`**: find the first pulse near the trigger
   point, then look in `[echo_min_us, echo_max_us]` after it for a second pulse whose
   normalized correlation with the first is >= `echo_corr_threshold`. Optional amplitude
   ratio gate. Keep on match, discard otherwise, re-arm.
3. **Config** (new `EchoValidationConfig`, QSettings under `ratemeter/echo_*`):
   `enabled`, `echo_min_us` (default 80), `echo_max_us` (default 150), `echo_corr_threshold`
   (default 0.8), `amp_ratio_min` (default 0 = off).
4. **Saved data**: keep validated segments with their metadata (first-pulse time, echo lag,
   correlation, amplitude ratio) for later analysis.

## Caveats to handle

- **Discarded segments still cost hardware memory.** The validator reduces saved data and
  analysis noise, not scope dead time. Set runt thresholds to keep false triggers rare.
- **The echo can trigger its own segment** (~112 us later, if re-arm time allows). Use
  `ps4000GetValuesTriggerTimeOffsetBulk` (or the `64` variant) to timestamp segments and
  drop a segment whose "first" pulse is the validated echo of the previous one.
- **Rare particles can stall a batch** (`IsReady` never goes true). Add a timeout: call
  `ps4000Stop`, read `ps4000GetNoOfCaptures`, and fetch only the captured segments.
- Rate counting: decide whether the ratemeter counts **validated** events only (recommended)
  or all triggers; expose both counts so the false-trigger rate is visible.

## Noise calibration widget (set runt thresholds from measured background)

Purpose: while the instrument is idle, measure background noise with the **current**
ratemeter channel settings and report the lowest runt/edge threshold that avoids
triggering on noise.

**UI**: a "Measure noise" button in the Trigger panel (next to `spin_trigger_threshold`),
with a result label (RMS, MAD-based sigma, peak-to-peak, suggested lower threshold,
estimated false-trigger rate). Disabled while the ratemeter or DAQ page is running
(reuse the existing `daq_busy` / mutual-exclusion logic). Button needs a connected scope.

**Acquisition**: a plain block capture through `PicoScopeService` (trigger off or
auto-trigger), using the ratemeter's current channel, range, probe, coupling, bandwidth
limit and sample interval, since noise depends on all of them. Capture a long window
(tens of ms, or several blocks) so the estimate is stable. Run it in a worker thread so the
UI never blocks.

**Statistics** (new pure function, e.g. `services/noise_stats.py`, unit-testable offline):
- RMS about the mean, in mV, plus peak-to-peak and largest excursion.
- Robust sigma from MAD (`1.4826 * median(|x - median(x)|)`), computed after a light
  high-pass / detrend. Show both: plain RMS can overstate noise when slow structure is
  present (channel B in `1900V_reflec_43.csv` has about +/-0.2 mV of slow structure in
  its pre-trigger region).
- **Suggested lower threshold** = `k * sigma_robust`, default `k = 5`, plus hysteresis.
- **False-trigger rate estimate**: count actual crossings of the suggested threshold in the
  captured noise and report them per second. Do not assume Gaussian noise.

**Caveats**
- Measure under realistic conditions. Noise may change with the 1900 V supply, pumps or
  gauges on; a cold measurement can understate it. Allow repeat measurements and show the
  time of the last one.
- The 4262's smallest range is +/-10 mV; report noise in mV and note the range used so the
  threshold step size is clear.
- Result is advisory only. It must not change trigger settings automatically.

**Tests**: synthetic Gaussian noise with known sigma (RMS and MAD agree within tolerance);
noise plus slow drift (MAD stays close to true sigma while plain RMS inflates); crossing
count matches a brute-force count; offline run on the pre-trigger region of
`1900V_reflec_43.csv`.

## Verification

- Replay `1900V_reflec_43.csv` through the validator offline: must accept it at the default
  window and reject it when the window excludes ~112 us.
- Synthetic tests: no echo, echo outside window, echo at 60% amplitude, inverted echo,
  noise-only segments.
- Hardware: compare accepted vs. rejected segment counts, and confirm no echo-triggered
  duplicates remain.

## Open items

- Exact runt enum values / struct layout from `ps4000Api.h` (needed before coding Step 0).
- Whether the ratemeter's rate logic should count validated events only.
- Calibrate the A/B amplitude ratio (~0.23) across many captures and particle velocities.