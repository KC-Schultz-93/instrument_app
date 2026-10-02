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