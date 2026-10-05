# Matched Filter Detection — Claude Code Handoff

## Context

This plan extends the existing instrument app with a matched filter detection
mode for identifying image charge signals from the CDMS instrument. It
supplements `ratemeter_plan.md` — read that document first.

The instrument produces bipolar image charge pulses after a 2nd order high-pass
filter. The signal shape is a single cycle of a sine wave (positive lobe
followed by negative lobe, or vice versa depending on charge polarity). The
period of this bipolar pulse is 78–92 µs. Background noise amplitude may exceed
the signal amplitude, but noise does not consistently produce a bipolar pulse
with this period — that timing constraint is the primary discriminator.

The goal is to detect these signals even when they fall below a simple voltage
threshold, and then bin detected events by their amplitude into the ratemeter's
existing band system.

---

## Design Overview

Detection works in three stages per waveform window:

1. **Matched filter** — cross-correlate the waveform against a synthetic
   bipolar template. The correlation output peaks sharply where a matching shape
   is present, regardless of raw amplitude.

2. **Period validation** — at each correlation peak, measure the zero-crossing
   interval of the underlying bipolar pulse. Accept only events where the
   zero-crossing period falls within `[period_min_us, period_max_us]` (default
   78–92 µs). This rejects noise correlation hits.

3. **Amplitude extraction** — at validated event locations, read the amplitude
   of the positive lobe from the original (baseline-corrected) waveform. This
   amplitude feeds into the ratemeter band system unchanged.

---

## Files to Create

```
instrument_app/
  services/
    matched_filter.py          ← new
```

## Files to Modify

```
instrument_app/
  services/daq_models.py       ← add MatchedFilterConfig, BipolarEventRecord
  services/ratemeter_worker.py ← add matched filter detection path
  pages/ratemeter_page.py      ← add matched filter UI controls
```

---

## Step 1 — `services/daq_models.py`

Add two dataclasses after `AmplitudeBand` and `RatemeterConfig`.

```python
@dataclass
class MatchedFilterConfig:
    """
    Configuration for matched filter bipolar pulse detection.

    The synthetic template is generated from period_us and sample_interval_ns
    at runtime — no external template file is needed until empirical data is
    available.
    """
    enabled: bool = False

    # Template parameters
    period_us: float = 85.0             # center estimate for template generation
    period_min_us: float = 78.0         # validation window lower bound
    period_max_us: float = 92.0         # validation window upper bound
    polarity: str = "positive_first"    # "positive_first" or "negative_first"
                                        # or "both" to check both orientations

    # Detection thresholds
    correlation_threshold: float = 0.35 # normalized cross-correlation score (0–1)
                                        # events below this score are rejected
    min_distance_us: float = 50.0       # minimum separation between detected
                                        # events in microseconds (prevents
                                        # double-counting one event)

    # Template source
    use_empirical_template: bool = False
    empirical_template_path: str = ""   # path to .npy file; ignored when
                                        # use_empirical_template is False


@dataclass
class BipolarEventRecord:
    """One detected bipolar image charge event."""
    event_index: int          # sample index of positive lobe peak in waveform
    time_us: float            # time from trace start, microseconds
    amplitude_v: float        # positive lobe amplitude, baseline-corrected volts
    correlation_score: float  # normalized cross-correlation score at detection
    period_us: float          # measured zero-crossing period of this event
    polarity: str             # "positive_first" or "negative_first"
```

---

## Step 2 — `services/matched_filter.py`

New file. No Qt, no hardware, no file I/O. Pure numpy/scipy signal processing.

```python
"""
matched_filter.py
-----------------
Matched filter detection of bipolar image charge pulses.

Stateless between traces — instantiate once per run and reuse.
No Qt, no hardware access, no file I/O.

Detection pipeline per waveform:
  1. Generate (or load) bipolar template
  2. Normalized cross-correlate template against waveform
  3. Find peaks in correlation output above threshold
  4. Validate each hit by measuring zero-crossing period
  5. Return BipolarEventRecord list with amplitude read from raw waveform
"""
```

### Class: `MatchedFilter`

```python
class MatchedFilter:
    def __init__(self, config: MatchedFilterConfig, sample_interval_ns: int):
        ...
```

Constructor builds or loads the template immediately so it isn't rebuilt per
trace. Store as `self._template` and `self._template_neg` (flipped polarity).

#### Template generation

```python
def _build_synthetic_template(
    self,
    period_us: float,
    sample_interval_ns: int,
    polarity: str,
) -> np.ndarray:
    """
    Generate a single-cycle sine wave template.

    Returns a normalized (zero-mean, unit-max) 1-D float64 array.
    Length = int(period_us * 1000 / sample_interval_ns) samples.

    polarity "positive_first": sin(t) over [0, 2pi]
    polarity "negative_first": -sin(t) over [0, 2pi]
    """
    n = int(period_us * 1_000 / sample_interval_ns)
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    template = np.sin(t)
    if polarity == "negative_first":
        template = -template
    # Zero-mean, normalize to unit max amplitude (correlation score is
    # amplitude-independent — shape only)
    template -= template.mean()
    template /= np.max(np.abs(template))
    return template
```

#### Normalized cross-correlation

Use `scipy.signal.correlate` in `mode="same"` so the output is the same length
as the input waveform and index alignment is preserved.

```python
def _normalized_correlate(
    self,
    waveform: np.ndarray,
    template: np.ndarray,
) -> np.ndarray:
    """
    Compute normalized cross-correlation between waveform and template.

    Output range is approximately [-1, 1]. Values near 1 indicate a strong
    match to the template shape at that position.

    Normalization divides by the local RMS of the waveform in a window
    matching the template length, so the score is amplitude-independent.
    """
    from scipy.signal import correlate

    n_t = len(template)
    raw_corr = correlate(waveform, template, mode="same")

    # Local RMS normalization: slide a window of len(template) over the waveform
    waveform_sq = waveform ** 2
    cs = np.cumsum(waveform_sq)
    cs = np.concatenate(([0], cs))
    half = n_t // 2
    left = np.maximum(0, np.arange(len(waveform)) - half)
    right = np.minimum(len(waveform), np.arange(len(waveform)) - half + n_t)
    window_energy = cs[right] - cs[left]
    local_rms = np.sqrt(np.maximum(window_energy / n_t, 1e-12))

    template_rms = np.sqrt(np.mean(template ** 2))
    normalized = raw_corr / (local_rms * template_rms * n_t)
    return np.clip(normalized, -1.0, 1.0)
```

#### Zero-crossing period measurement

After a correlation peak is found at index `i`, extract a waveform window
of length `2 * template_len` centered on `i` and measure the zero-crossing
interval:

```python
def _measure_period(
    self,
    waveform: np.ndarray,
    peak_index: int,
    template_len: int,
    sample_interval_ns: int,
) -> float:
    """
    Measure the zero-crossing period of a bipolar pulse at peak_index.

    Extracts a window around the event, finds the zero crossings bounding
    the positive and negative lobes, and returns the full period in us.

    Returns NaN if fewer than 2 zero crossings are found in the window.
    """
    half = template_len
    start = max(0, peak_index - half // 2)
    end = min(len(waveform), start + template_len)
    window = waveform[start:end]

    # Find zero crossings: indices where sign changes
    signs = np.sign(window)
    signs[signs == 0] = 1   # treat exact zero as positive
    crossings = np.where(np.diff(signs))[0]

    if len(crossings) < 2:
        return float("nan")

    # Period = time between first and last zero crossing in window
    period_samples = crossings[-1] - crossings[0]
    period_us = period_samples * sample_interval_ns / 1_000
    return period_us
```

#### Main detection method

```python
def detect(
    self,
    voltage: np.ndarray,
    time_ns: np.ndarray,
) -> list[BipolarEventRecord]:
    """
    Run matched filter detection on one baseline-corrected waveform.

    Parameters
    ----------
    voltage : ndarray
        Baseline-corrected voltage array (volts).
    time_ns : ndarray
        Time array in nanoseconds, same length as voltage.

    Returns
    -------
    List[BipolarEventRecord]
        Detected events sorted by time, validated by period.
    """
```

Internal logic:

1. Run `_normalized_correlate` against `self._template`
2. If `config.polarity == "both"`, also run against `self._template_neg`;
   take element-wise maximum of the two correlation arrays and record which
   template won at each position
3. Find peaks in the correlation array above `config.correlation_threshold`
   using `scipy.signal.find_peaks` with `distance` set to
   `int(config.min_distance_us * 1000 / sample_interval_ns)`
4. For each correlation peak at index `i`:
   a. Call `_measure_period(voltage, i, len(self._template), sample_interval_ns)`
   b. If period is NaN or outside `[period_min_us, period_max_us]` → skip
   c. Read amplitude: `amplitude_v = float(voltage[i])` (positive lobe for
      `positive_first`; for `negative_first`, find local minimum in half-window
      before `i` and negate)
   d. Append `BipolarEventRecord`
5. Return sorted list

---

## Step 3 — `services/ratemeter_worker.py` modifications

Add matched filter as an optional detection path. The worker receives both
`RatemeterConfig` and `MatchedFilterConfig`.

### Constructor change

```python
def __init__(
    self,
    service: PicoScopeService,
    config: RatemeterConfig,
    mf_config: MatchedFilterConfig,          # new
    trigger_enabled: bool,
    trigger_threshold_v: float,
    trigger_direction: str,
    parent=None,
):
    ...
    self._mf_config = mf_config
    if mf_config.enabled:
        from instrument_app.services.matched_filter import MatchedFilter
        self._matcher = MatchedFilter(mf_config, config.sample_interval_ns)
    else:
        self._matcher = None
```

### Per-trace detection logic

Replace the existing peak-finding block with a branch:

```python
if self._matcher is not None:
    # --- Matched filter path ---
    events = self._matcher.detect(corrected_voltage, record.time_ns)
    for evt in events:
        amp_mv = evt.amplitude_v * 1000
        for band in self._config.bands:
            if band.low_mv <= amp_mv <= band.high_mv:
                self._hit_times[band.label].append(now)
else:
    # --- Existing simple threshold path ---
    peaks = self._extractor.find_peaks(
        corrected_voltage, baseline_mean, baseline_rms, record.time_ns
    )
    for peak in peaks:
        amp_mv = peak.amplitude_v * 1000
        for band in self._config.bands:
            if band.low_mv <= amp_mv <= band.high_mv:
                self._hit_times[band.label].append(now)
```

Both paths feed the same `_hit_times` deque and `rates_updated` signal — the
rest of the worker is unchanged.

---

## Step 4 — `pages/ratemeter_page.py` modifications

Add a **Detection Mode** group to the left panel, inserted between the Trigger
group and the Averaging group.

### New group box: Detection Mode

```
+- Detection Mode -----------------------------------------------+
|  Mode: [Simple threshold v]                                    |
|         Simple threshold                                       |
|         Matched filter (bipolar)                               |
|                                                                |
|  -- Matched filter settings --                                 |
|  Template period (us):  [85.0            ]                     |
|  Period min (us):       [78.0            ]                     |
|  Period max (us):       [92.0            ]                     |
|  Polarity: [Positive first v]                                  |
|  Correlation threshold: [0.35            ]                     |
|  Min event spacing (us):[50.0            ]                     |
|                                                                |
|  [i] Matched filter detects sub-threshold bipolar pulses      |
|      by shape, not amplitude.                                  |
+----------------------------------------------------------------+
```

Show/hide the matched filter settings block based on the mode combo selection.
Use `widget.setVisible(bool)` — do not add/remove widgets dynamically.

### Control specifications

| Control | Widget | Range | Default |
|---|---|---|---|
| Mode | `QComboBox` | Simple threshold, Matched filter | Simple threshold |
| Template period | `QDoubleSpinBox` | 10–500 µs | 85.0 µs |
| Period min | `QDoubleSpinBox` | 10–500 µs | 78.0 µs |
| Period max | `QDoubleSpinBox` | 10–500 µs | 92.0 µs |
| Polarity | `QComboBox` | Positive first, Negative first, Both | Positive first |
| Correlation threshold | `QDoubleSpinBox` | 0.05–0.99, step 0.01 | 0.35 |
| Min event spacing | `QDoubleSpinBox` | 10–1000 µs | 50.0 µs |

### Auto-apply on change

All matched filter controls follow the same auto-apply + 300 ms debounce
pattern as the acquisition controls — changing any value while running triggers
`_restart_worker()`.

### `MatchedFilterConfig` construction

```python
def _build_mf_config(self) -> MatchedFilterConfig:
    mode = self.combo_detection_mode.currentText()
    enabled = mode == "Matched filter (bipolar)"
    polarity_map = {
        "Positive first": "positive_first",
        "Negative first": "negative_first",
        "Both":           "both",
    }
    return MatchedFilterConfig(
        enabled=enabled,
        period_us=self.spin_mf_period.value(),
        period_min_us=self.spin_mf_period_min.value(),
        period_max_us=self.spin_mf_period_max.value(),
        polarity=polarity_map[self.combo_mf_polarity.currentText()],
        correlation_threshold=self.spin_mf_threshold.value(),
        min_distance_us=self.spin_mf_min_spacing.value(),
        use_empirical_template=False,
        empirical_template_path="",
    )
```

Pass `_build_mf_config()` result alongside `_build_ratemeter_config()` when
constructing `RatemeterWorker`.

### QSettings persistence

Add to the `ratemeter/` key namespace:

| Key | Widget | Type |
|---|---|---|
| `ratemeter/detection_mode` | mode combo | str |
| `ratemeter/mf_period_us` | template period spin | float |
| `ratemeter/mf_period_min_us` | period min spin | float |
| `ratemeter/mf_period_max_us` | period max spin | float |
| `ratemeter/mf_polarity` | polarity combo | str |
| `ratemeter/mf_corr_threshold` | threshold spin | float |
| `ratemeter/mf_min_spacing_us` | min spacing spin | float |

---

## Step 5 — Waveform Plot: Correlation Score Overlay (optional but recommended)

When matched filter mode is active, add a second y-axis to the waveform plot
showing the normalized correlation score for the last trace. This lets the
operator see in real time which parts of the waveform are matching the template
and whether the threshold is set appropriately.

Implementation:

```python
# In ratemeter_page.py, inside _build_waveform_plot():
self._corr_plot = pg.ViewBox()
self._waveform_plot.scene().addItem(self._corr_plot)
self._waveform_plot.getAxis("right").linkToView(self._corr_plot)
self._waveform_plot.getAxis("right").setLabel("Correlation score", units="")
self._corr_curve = pg.PlotDataItem(pen=pg.mkPen("#ffffff", width=1, style=Qt.DotLine))
self._corr_plot.addItem(self._corr_curve)
```

Only show the right axis and correlation curve when matched filter mode is
active. Hide both when mode is "Simple threshold".

The `RatemeterWorker` needs one additional signal for this:

```python
# In ratemeter_worker.py:
correlation_ready = pyqtSignal(object)  # np.ndarray, normalized correlation
```

Emit it alongside `waveform_ready` when `self._matcher is not None`. The page
throttles both to ~10 Hz.

---

## Step 6 — Empirical Template Support (stub, implement later)

Add the infrastructure now so it can be filled in when real data is available.
Do not implement the averaging/alignment logic yet — just the load path.

In `MatchedFilter.__init__`:

```python
if config.use_empirical_template and config.empirical_template_path:
    path = Path(config.empirical_template_path)
    if path.exists() and path.suffix == ".npy":
        raw = np.load(path)
        raw -= raw.mean()
        raw /= np.max(np.abs(raw))
        self._template = raw
        if config.polarity == "both":
            self._template_neg = -raw
    else:
        # Fall back to synthetic and log a warning
        self._template = self._build_synthetic_template(...)
```

In the UI, add a disabled "Load empirical template (.npy)" file picker button
beneath the matched filter settings. Enable it only when
`use_empirical_template` is checked. Leave `use_empirical_template` unchecked
and grayed out for now — it is a placeholder for the future workflow where the
operator captures clean events and averages them into a template file.

---

## Build Order

1. `daq_models.py` — add `MatchedFilterConfig` and `BipolarEventRecord`
2. `matched_filter.py` — implement `MatchedFilter` class fully, including unit
   tests with a synthetic bipolar waveform (see testing notes below)
3. `ratemeter_worker.py` — add `mf_config` parameter and detection branch
4. `ratemeter_page.py` — add Detection Mode group, wire controls, update
   `_restart_worker()` to pass `_build_mf_config()`

---

## Testing Notes

Add `tests/test_matched_filter.py`. No hardware required — use synthetic
waveforms:

```python
def make_bipolar_trace(
    n_samples: int,
    sample_interval_ns: int,
    period_us: float,
    amplitude_v: float,
    noise_v: float,
    event_times_us: list[float],
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Generate a synthetic waveform containing bipolar pulses at specified times.
    Adds Gaussian noise at noise_v RMS. Returns (time_ns, voltage).
    """
```

Tests to cover:

- `test_detects_clean_bipolar` — single event, no noise, expect 1 result
- `test_detects_subthreshold_bipolar` — noise_v > amplitude_v (SNR < 1),
  expect simple threshold finds 0 peaks but matched filter finds 1 event
- `test_rejects_noise_spike` — single large Gaussian noise spike (not bipolar),
  expect 0 events from matched filter
- `test_period_validation_rejects_wrong_period` — inject bipolar pulse with
  period 60 µs (outside 78–92 window), expect 0 events
- `test_both_polarities` — test with `polarity="both"` and one event of each
  polarity in the same trace, expect 2 events
- `test_amplitude_extraction_accuracy` — verify `BipolarEventRecord.amplitude_v`
  matches injected amplitude to within 10% in low-noise conditions

---

## Important Implementation Notes

1. **`scipy.signal.correlate` mode.** Use `mode="same"` so the output array is
   the same length as the input waveform and indices map directly to waveform
   sample positions. Do not use `mode="full"`.

2. **Template length vs. window duration.** The template is ~425 samples at
   200 ns / 85 µs. The waveform window must be meaningfully longer than the
   template or the correlation at the edges will be unreliable. Enforce a
   minimum: `window_duration_ms >= period_max_us * 3 / 1000`. Add a warning
   label in the UI if this constraint is violated.

3. **Baseline correction is mandatory before `detect()`.** The matched filter
   assumes zero-mean input. Always run `WaveformProcessor.estimate_baseline` +
   `subtract_baseline` before calling `MatchedFilter.detect()`, same as the
   existing threshold path.

4. **Correlation threshold tuning.** 0.35 is a reasonable starting value for
   a clean synthetic template against a noisy signal. When the empirical
   template is loaded, this threshold can typically be raised (0.5–0.7) because
   the shape match is tighter. Expose it prominently in the UI.

5. **`polarity="both"` cost.** Running correlation twice doubles the compute
   per trace. At 200 ns sample interval and 5 ms windows (~25,000 samples),
   this is still well under 1 ms on modern hardware. No optimization needed.

6. **Do not import `matched_filter.py` at module level in `ratemeter_worker.py`.**
   Import inside `__init__` only when `mf_config.enabled` is True. This keeps
   the worker importable in test environments where scipy may behave differently.

7. **`BipolarEventRecord` is not logged.** The ratemeter page is a live
   diagnostic tool. Do not write `BipolarEventRecord` data to disk. The
   amplitude value is consumed by the band system and discarded after rate
   calculation, same as `PeakRecord` in the simple threshold path.

# Matched Filter — Multi-Scale Extension (Addendum)

This document extends `match_filter.md` (above) with multi-scale template
matching to handle particles with a wide range of transit times (pulse
durations). The core matched filter architecture is unchanged; this adds a
scale-loop around the existing `detect()` pipeline.

## Background

The empirical template (`template.npy`) was built from captures with pulse
durations of approximately 75–90 µs (peak-to-peak). Particles traveling at
different velocities produce the same bipolar shape but stretched or compressed
in time. A scalar multiplier maps directly to the stretch factor:

- `scale = 1.0` → template as-is (~85 µs pulse)
- `scale = 2.0` → stretched to ~170 µs (slower particle)
- `scale = 0.5` → compressed to ~42 µs (faster particle)

Amplitude variation across particle sizes/charges is already handled by the
normalized cross-correlation — the scale extension adds velocity-range coverage.

---

## Changes to `daq_models.py`

Add `scale_factors` to `MatchedFilterConfig`:

```python
@dataclass
class MatchedFilterConfig:
    enabled: bool = False
    pulse_duration_us: float = 85.0      # renamed from period_us — see note below
    pulse_min_us: float = 60.0           # renamed from period_min_us
    pulse_max_us: float = 110.0          # renamed from period_max_us
    polarity: str = "positive_first"
    correlation_threshold: float = 0.35
    min_distance_us: float = 50.0
    use_empirical_template: bool = False
    empirical_template_path: str = ""
    # --- NEW ---
    scale_factors: List[float] = field(default_factory=lambda: [1.0])
    # e.g. [0.5, 1.0, 1.5, 2.0] to sweep four candidate durations
```

**Naming note:** `period_us`, `period_min_us`, `period_max_us` should be
renamed to `pulse_duration_us`, `pulse_min_us`, `pulse_max_us` throughout to
reflect that this value is the full bipolar pulse duration (peak-to-peak time),
which is a half-period in sine terms — not a full period. This prevents future
confusion. Update all references in `matched_filter.py` and
`ratemeter_page.py`.

---

## Changes to `services/matched_filter.py`

### 1. Template rescaling

Add a helper that resamples the template to a target length using
`scipy.ndimage.zoom` (preserves shape better than simple slicing):

```python
from scipy.ndimage import zoom as ndimage_zoom

def _scale_template(self, template: np.ndarray, scale: float) -> np.ndarray:
    """
    Stretch (scale > 1) or compress (scale < 1) the template by resampling.
    Returns a zero-mean, unit-max normalized copy at the new length.
    """
    if abs(scale - 1.0) < 1e-6:
        return template.copy()
    scaled = ndimage_zoom(template, scale, order=3)   # cubic interpolation
    scaled -= np.mean(scaled)
    peak = np.max(np.abs(scaled))
    if peak > 0:
        scaled /= peak
    return scaled.astype(np.float32)
```

### 2. Multi-scale `detect()` loop

Replace the single-template correlation in `detect()` with a loop over
`config.scale_factors`. For each candidate scale, run the full correlation and
collect candidate hits. After all scales, merge hits (deduplication by
proximity), then apply `min_distance_us` suppression on the merged list.

```python
def detect(self, waveform: np.ndarray, time_us: np.ndarray,
           config: MatchedFilterConfig) -> List[BipolarEventRecord]:

    waveform = self._preprocess(waveform)   # baseline subtract, existing method
    all_candidates = []                      # (time_us, score, scale) tuples

    for scale in config.scale_factors:
        tmpl = self._scale_template(self._template, scale)
        corr = self._normalized_correlate(waveform, tmpl)

        # Expected pulse duration at this scale
        expected_dur_us = self.config.pulse_duration_us * scale

        # Find correlation peaks above threshold
        min_dist_samples = int(config.min_distance_us /
                               np.median(np.diff(time_us)))
        peak_indices, props = scipy.signal.find_peaks(
            corr,
            height=config.correlation_threshold,
            distance=max(1, min_dist_samples),
        )

        for idx in peak_indices:
            # Period/duration gate: measure zero-crossing span at this location
            dur = self._measure_pulse_duration(waveform, time_us, idx)
            dur_min = expected_dur_us * 0.7   # ±30% tolerance
            dur_max = expected_dur_us * 1.3
            if not (dur_min <= dur <= dur_max):
                continue

            all_candidates.append((
                float(time_us[idx]),
                float(corr[idx]),
                scale,
                idx,
            ))

    if not all_candidates:
        return []

    # Deduplicate: if two scales detect the same event (within min_distance_us),
    # keep the one with the higher correlation score
    all_candidates.sort(key=lambda c: c[0])   # sort by time
    merged = []
    for cand in all_candidates:
        if merged and (cand[0] - merged[-1][0]) < config.min_distance_us:
            if cand[1] > merged[-1][1]:        # replace if better score
                merged[-1] = cand
        else:
            merged.append(cand)

    # Build BipolarEventRecord for each surviving candidate
    records = []
    for t_us, score, scale, idx in merged:
        amp = self._extract_amplitude(waveform, time_us, idx, config.polarity)
        dur = self._measure_pulse_duration(waveform, time_us, idx)
        records.append(BipolarEventRecord(
            event_index=len(records),
            time_us=t_us,
            amplitude_v=amp,
            correlation_score=score,
            period_us=dur,        # actual measured duration, not scaled target
            polarity=self._detect_polarity(waveform, idx),
            matched_scale=scale,  # NEW field — see below
        ))

    return records
```

### 3. Add `matched_scale` to `BipolarEventRecord`

```python
@dataclass
class BipolarEventRecord:
    event_index: int
    time_us: float
    amplitude_v: float
    correlation_score: float
    period_us: float          # actual measured pulse duration
    polarity: str
    matched_scale: float = 1.0   # NEW — which scale factor produced this hit
```

This lets the ratemeter page (or future analysis) know which particle velocity
class each event belongs to.

---

## Changes to `pages/ratemeter_page.py`

### UI controls to add (inside the Detection Mode group box, matched filter section)

**Scale factors input** — a line edit accepting a comma-separated list:

```
Scale factors:  [ 0.75, 1.0, 1.5, 2.0 ]   (QLineEdit, validated on change)
```

- Default: `"1.0"` (preserves existing single-scale behavior)
- Validation: parse as floats, reject non-positive values, cap list at 8
  entries (8 correlation passes per acquisition window is practical maximum)
- On change: update `MatchedFilterConfig.scale_factors`, persist to QSettings
  under key `ratemeter/mf_scale_factors`
- Show a small label next to the field showing the equivalent pulse durations:
  e.g. `"→ 42, 85, 128, 170 µs"` — computed as
  `pulse_duration_us * scale` for each entry

### QSettings key

```
ratemeter/mf_scale_factors   →  JSON list of floats, e.g. [0.75, 1.0, 1.5, 2.0]
```

---

## Performance note

Each additional scale factor adds one full correlation pass per acquired
waveform. At the ratemeter's typical acquisition rate this is negligible for
≤8 scales, but the UI should show a warning label if `len(scale_factors) > 8`:

```
"⚠ More than 8 scales may cause acquisition lag"
```

---

## Unit tests to add (`tests/test_matched_filter.py`)

1. `test_scale_1_0_matches_baseline` — scale=1.0 gives same result as
   unscaled template on a synthetic pulse of the nominal duration
2. `test_scale_2_0_detects_stretched_pulse` — a pulse at 2× duration is
   missed at scale=1.0, detected at scale=2.0
3. `test_deduplication_keeps_best_score` — two scales detecting the same
   event within `min_distance_us` → only the higher-score hit survives
4. `test_matched_scale_recorded` — `BipolarEventRecord.matched_scale` reflects
   which scale factor produced the hit
5. `test_empty_scale_list_raises` — `scale_factors=[]` raises `ValueError`
   before running detection

---

## Implementation order

1. Rename `period_us` → `pulse_duration_us` throughout (low risk, do first)
2. Add `matched_scale` to `BipolarEventRecord`
3. Add `scale_factors` to `MatchedFilterConfig` with default `[1.0]`
4. Implement `_scale_template()` in `MatchedFilter`
5. Refactor `detect()` with scale loop + deduplication
6. Add UI controls to `ratemeter_page.py`
7. Add unit tests