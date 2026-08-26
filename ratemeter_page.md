# Ratemeter Page — Claude Code Handoff

## Context

This is a Python/PyQt5 instrument control app (`instrument_app`) for a CDMS
(Charge Detection Mass Spectrometry) lab setup using a PicoScope 4262.

The app already has:
- `pages/daq_page.py` — full DAQ acquisition page (reference implementation for layout patterns)
- `services/picoscope_service.py` — all PicoSDK calls, owns the device handle
- `services/acquisition_worker.py` — QThread worker, block-mode acquisition loop
- `services/waveform_processor.py` — baseline estimation, subtraction
- `services/signal_extractor.py` — peak finding (`find_peaks` → `List[PeakRecord]`)
- `services/daq_models.py` — shared dataclasses (`AcquisitionConfig`, `WaveformRecord`, `PeakRecord`, etc.)
- `services/daq_channels.py` — Qt signal bus shared across DAQ subsystem
- `theme/style.py` + `theme/themes.py` — theming system (`style` proxy, `Theme` dataclass)
- `ui/primitives.py` — `ThemedButton`, `PillLabel`, etc.
- `app/main.py` — `MainWindow`, tab registration

**Architecture rules (do not break these):**
- `pages/` calls `services/` — never the reverse
- Each `services/` module owns one concern — do not combine concerns
- `services/serial_manager.py` and `services/data_recorder.py` — **do not touch**
- DAQ and serial/pressure subsystems must remain decoupled
- Always use a `QThread` worker for acquisition — never block the Qt UI thread
- Only one page may hold the PicoScope handle at a time

---

## Goal

Add a **Ratemeter** tab to the app. Its purpose is to act as a live diagnostic
tool while the operator adjusts ion optic voltages: it shows how frequently
particle signals are occurring within user-defined amplitude bands (e.g.
monomers vs. dimers vs. trimers), plus a live waveform strip for visual
confirmation.

No data logging. No CDMS physics. Stateless between runs.

---

## Files to Create

```
instrument_app/
  pages/
    ratemeter_page.py          ← new
  services/
    ratemeter_worker.py        ← new
```

## Files to Modify

```
instrument_app/
  services/daq_models.py       ← add AmplitudeBand, RatemeterConfig dataclasses
  app/main.py                  ← register tab, add mutual-exclusion guard
  services/daq_channels.py     ← add daq_busy signal (see below)
```

---

## Step 1 — `services/daq_models.py`

Add two dataclasses. Place them after the existing dataclass definitions.
Do not modify any existing dataclasses.

```python
from typing import List   # already imported; confirm before adding

@dataclass
class AmplitudeBand:
    """One user-defined amplitude window for ratemeter counting."""
    label: str          # e.g. "Band 1", "Band 2"
    low_mv: float       # lower bound, millivolts (inclusive)
    high_mv: float      # upper bound, millivolts (inclusive)
    color: str          # hex color string, e.g. "#4fc3f7"


@dataclass
class RatemeterConfig:
    """Full configuration for one ratemeter run."""
    channel: str                    # "A"
    voltage_range_v: float          # e.g. 0.02 for ±20 mV
    coupling: str                   # "DC" or "AC"
    sample_interval_ns: int         # e.g. 200
    window_duration_ms: float       # length of each acquisition window
    rate_averaging_s: float         # rolling window for Hz calculation
    bands: List[AmplitudeBand]

    @property
    def num_samples(self) -> int:
        return max(1, int(self.window_duration_ms * 1e6 / self.sample_interval_ns))

    def to_acquisition_config(
        self,
        trigger_enabled: bool = False,
        trigger_threshold_v: float = 0.0,
        trigger_direction: str = "RISING",
    ) -> "AcquisitionConfig":
        return AcquisitionConfig(
            channel=self.channel,
            voltage_range_v=self.voltage_range_v,
            coupling=self.coupling,
            sample_interval_ns=self.sample_interval_ns,
            num_samples=self.num_samples,
            trigger_enabled=trigger_enabled,
            trigger_threshold_v=trigger_threshold_v,
            trigger_direction=trigger_direction,
        )
```

---

## Step 2 — `services/daq_channels.py`

Add one signal so pages can signal each other when the PicoScope is busy.
Read the existing file first and add the signal alongside the existing ones —
do not restructure the class.

```python
# Add to DAQChannels:
daq_busy = pyqtSignal(bool)   # True = PicoScope in use, False = released
```

---

## Step 3 — `services/ratemeter_worker.py`

New file. Model the class structure on `acquisition_worker.py`.

```python
"""
ratemeter_worker.py
-------------------
QThread worker for the Ratemeter page.

Runs a continuous block-mode acquisition loop and, per waveform window:
  1. Estimates and subtracts baseline
  2. Finds peaks (reuses SignalExtractor)
  3. Bins each peak into whichever AmplitudeBand its amplitude falls in
  4. Emits rolling rates (Hz) for each band every ~0.5 s wall-clock

No file I/O, no CDMS physics, no logging.
"""
```

### Signals

```python
rates_updated    = pyqtSignal(object)   # dict[str, float]  band_label → Hz
waveform_ready   = pyqtSignal(object)   # WaveformRecord  (latest trace, for plot)
error_occurred   = pyqtSignal(str)
status_update    = pyqtSignal(str)
trace_count_changed = pyqtSignal(int)   # total windows acquired
```

### Constructor

```python
def __init__(
    self,
    service: PicoScopeService,
    config: RatemeterConfig,
    trigger_enabled: bool,
    trigger_threshold_v: float,
    trigger_direction: str,
    parent=None,
)
```

Instantiate `SignalExtractor()` and `WaveformProcessor` once; reuse per trace.

### Rolling rate calculation

Use `collections.deque` — one deque per band, storing `(timestamp_s: float, hit: int)`
tuples where `hit` is always 1. On each rate emission:

```python
import time
from collections import deque

# Per band, at worker init:
self._hit_times: dict[str, deque] = {b.label: deque() for b in config.bands}

# Per trace, after peak binning:
now = time.monotonic()
for band in self._config.bands:
    for peak in peaks:
        amp_mv = peak.amplitude_v * 1000
        if band.low_mv <= amp_mv <= band.high_mv:
            self._hit_times[band.label].append(now)

# Prune old entries and emit rate every ~0.5 s:
cutoff = now - self._config.rate_averaging_s
rates = {}
for band in self._config.bands:
    dq = self._hit_times[band.label]
    while dq and dq[0] < cutoff:
        dq.popleft()
    elapsed = min(now - self._loop_start, self._config.rate_averaging_s)
    rates[band.label] = len(dq) / elapsed if elapsed > 0 else 0.0
self.rates_updated.emit(rates)
```

Emit `waveform_ready` for every trace (the page throttles display to ~10 Hz
itself, same as DAQPage).

### Stop mechanism

Same pattern as `AcquisitionWorker`:
```python
def request_stop(self) -> None:
    self._stop_flag = True
```

---

## Step 4 — `pages/ratemeter_page.py`

New file. Follow the same structure as `daq_page.py`.

### Layout

Horizontal `QSplitter`:
- **Left panel** — fixed 320 px, scrollable `QScrollArea` wrapping a `QVBoxLayout`
- **Right panel** — expandable, contains waveform plot (top) and rate display (bottom)

```
┌─ LEFT PANEL (320 px) ──┬─ RIGHT PANEL ──────────────────────────────────┐
│                        │                                                 │
│  ┌─ Connection ──────┐ │  ┌─ Waveform ────────────────────────────────┐ │
│  │ [Connect] [Discon]│ │  │  pyqtgraph PlotWidget                     │ │
│  │ Status: ...       │ │  │  - Last acquired trace, updates ~10 Hz    │ │
│  └───────────────────┘ │  │  - Horizontal dashed lines per band       │ │
│                        │  │    (low and high bounds, band color)      │ │
│  ┌─ Acquisition ─────┐ │  │  - Trigger threshold line if enabled      │ │
│  │ Window: [5.0 ms ] │ │  └───────────────────────────────────────────┘ │
│  │ Sample: [200 ns▾] │ │                                                 │
│  │ Range:  [±20 mV▾] │ │  ┌─ Live Rates ──────────────────────────────┐ │
│  │ Coupling: [DC  ▾] │ │  │  Band 1   5.0–7.0 mV    ██  12.4 Hz     │ │
│  └───────────────────┘ │  │  Band 2  11.0–14.0 mV   █    3.1 Hz     │ │
│                        │  │  Band 3   ...                             │ │
│  ┌─ Trigger ─────────┐ │  └───────────────────────────────────────────┘ │
│  │ [✓] Enable        │ │                                                 │
│  │ Threshold: [6 mV] │ │  ┌─ Rate Trend ──────────────────────────────┐ │
│  │ Direction: [Rise▾]│ │  │  pyqtgraph PlotWidget                     │ │
│  │ Auto: [1000 ms  ] │ │  │  - One line per band, color-matched       │ │
│  └───────────────────┘ │  │  - X: rolling last N seconds              │ │
│                        │  │  - Y: Hz (min 0)                          │ │
│  ┌─ Averaging ───────┐ │  │  - Legend upper-right                     │ │
│  │ Window: [10 s   ] │ │  └───────────────────────────────────────────┘ │
│  │ Trend: [60 s    ] │ │                                                 │
│  └───────────────────┘ │                                                 │
│                        │                                                 │
│  ┌─ Bands ───────────┐ │                                                 │
│  │ [QTableWidget]    │ │                                                 │
│  │ # | Low | High |⬛│ │                                                 │
│  │ 1 | 5.0 | 7.0  |⬛│ │                                                 │
│  │ [+ Add] [- Remove]│ │                                                 │
│  └───────────────────┘ │                                                 │
│                        │                                                 │
│  [  Start  ] [  Stop  ]│                                                 │
│  Status label          │                                                 │
└────────────────────────┴─────────────────────────────────────────────────┘
```

### Waveform Plot

Use `pyqtgraph.PlotWidget`. On each `waveform_ready` signal (throttled to
~10 Hz with `time.monotonic()`):
- Plot voltage (mV on Y-axis, time in µs on X-axis)
- Redraw `InfiniteLine` objects for each band's `low_mv` and `high_mv` bounds
  in the band's color, style `Qt.DashLine`
- If trigger is enabled, draw trigger threshold as a white `InfiniteLine`

### Live Rate Display

A `QFrame` below the waveform plot. One row per band, dynamically created
when bands are added/removed:

```
  ■  Band 1   5.0 – 7.0 mV      12.4 Hz
  ■  Band 2  11.0 – 14.0 mV      3.1 Hz
```

- Colored square (or `PillLabel`) matching band color
- Band label + range as gray text
- Rate as large bold text (18 pt `Consolas` or `Segoe UI`), color = band color
- Update on every `rates_updated` signal

### Rate Trend Plot

A second `pyqtgraph.PlotWidget`. Maintains a circular buffer per band:

```python
from collections import deque
_TREND_MAXPOINTS = 600  # 60 s at 10 Hz update

self._trend_times: dict[str, deque] = {}
self._trend_rates: dict[str, deque] = {}
```

On each `rates_updated`:
1. Append `(now, rate)` to each band's deque
2. Drop entries older than `trend_window_s`
3. Update the `PlotDataItem` for each band

Add legend via `plot.addLegend()`.

### Band Table

`QTableWidget` with columns: `#`, `Low (mV)`, `High (mV)`, `Color`.

- `#` column: auto-numbered, not editable
- `Low` and `High`: editable `QDoubleSpinBox` delegates, or just plain editable
  cells (parse on commit)
- `Color` column: clicking opens `QColorDialog`; cell background shows the
  chosen color
- **Add Band** button: appends a new row, auto-assigns next number, picks a
  color from a preset cycle (see color cycle below)
- **Remove** button: removes selected row

Color cycle for auto-assignment (use in order, wrap around):
```python
_BAND_COLORS = [
    "#4fc3f7",  # light blue
    "#ff8a65",  # orange
    "#81c784",  # green
    "#ce93d8",  # purple
    "#fff176",  # yellow
    "#f48fb1",  # pink
    "#80cbc4",  # teal
    "#ffcc80",  # amber
]
```

### Auto-apply on Change

When any acquisition control changes (voltage range, sample interval, window
duration, coupling, trigger enable/threshold/direction/auto-timeout):

1. If worker is not running — do nothing (change takes effect at next Start)
2. If worker is running — call `_restart_worker()`:
   - `worker.request_stop()` → `worker.wait(3000)`
   - rebuild `RatemeterConfig` and `AcquisitionConfig` from current UI state
   - `reconfigure_channel()` + `set_trigger()` on the service
   - create and start a new worker

Debounce spin box changes with a 300 ms `QTimer` (single-shot) to avoid
restarting on every keystroke.

When bands are added/removed/edited while running, call `_restart_worker()`
immediately (no debounce needed — band changes are deliberate).

### Start / Stop

**Start:**
1. Check `daq_channels.daq_busy` — if True, show a `QMessageBox` warning
   ("PicoScope is in use by the DAQ page") and return
2. `service.connect()` if not connected
3. `service.configure_channel(acq_config)`
4. `service.set_trigger(acq_config)`
5. Build and start `RatemeterWorker`
6. Emit `daq_channels.daq_busy(True)`

**Stop:**
1. `worker.request_stop()` → `worker.wait(5000)`
2. `service.disconnect()`
3. Emit `daq_channels.daq_busy(False)`

### QSettings Persistence

Key prefix: `ratemeter/`

Save and restore on page init / close:

| Key | Widget | Type |
|---|---|---|
| `ratemeter/voltage_range_v` | voltage range combo | float |
| `ratemeter/sample_interval_ns` | sample interval combo | int |
| `ratemeter/window_duration_ms` | window duration spin | float |
| `ratemeter/coupling` | coupling combo | str |
| `ratemeter/trigger_enabled` | trigger checkbox | bool |
| `ratemeter/trigger_threshold_mv` | threshold spin | float |
| `ratemeter/trigger_direction` | direction combo | str |
| `ratemeter/trigger_auto_ms` | auto-timeout spin | int |
| `ratemeter/rate_averaging_s` | averaging spin | int |
| `ratemeter/trend_window_s` | trend window spin | int |
| `ratemeter/bands` | band table | JSON string |

Band JSON format:
```json
[
  {"label": "Band 1", "low_mv": 5.0, "high_mv": 7.0, "color": "#4fc3f7"},
  {"label": "Band 2", "low_mv": 11.0, "high_mv": 14.0, "color": "#ff8a65"}
]
```

Use `json.dumps` / `json.loads`. Wrap in try/except on load (corrupt settings
→ start with empty band list).

---

## Step 5 — `app/main.py`

### Register the tab

```python
from instrument_app.pages.ratemeter_page import RatemeterPage

# In _build_tabs(), after the DAQ tab:
self.ratemeter = RatemeterPage(self.daq_channels)
self.tabs.addTab(self.ratemeter, "Ratemeter")
```

### Mutual-exclusion guard

Connect the `daq_busy` signal so both pages see it:

```python
# In MainWindow.__init__, after building tabs:
self.daq_channels.daq_busy.connect(self.daq._on_daq_busy)
self.daq_channels.daq_busy.connect(self.ratemeter._on_daq_busy)
```

In `DAQPage`, add:
```python
def _on_daq_busy(self, busy: bool) -> None:
    """Disable Start when another page holds the PicoScope."""
    if busy and self._worker is None:
        self.btn_start.setEnabled(False)
        self.lbl_run_status.setText("PicoScope in use by Ratemeter")
    elif not busy and self._worker is None:
        self.btn_start.setEnabled(True)
        self.lbl_run_status.setText("Idle")
```

`DAQPage` must also emit `daq_busy` on its own start/stop — add:
```python
# In start_acquisition():
self.channels.daq_busy.emit(True)

# In stop_acquisition():
self.channels.daq_busy.emit(False)
```

In `RatemeterPage`, mirror the same `_on_daq_busy` handler.

### closeEvent

In `MainWindow.closeEvent`, add:
```python
if hasattr(self, "ratemeter"):
    if hasattr(self.ratemeter, "stop_acquisition"):
        self.ratemeter.stop_acquisition()
```

---

## Control Specifications

### Acquisition Controls

| Control | Widget | Range | Default | Notes |
|---|---|---|---|---|
| Window duration | `QDoubleSpinBox` | 0.1 – 1000 ms | 5.0 ms | Recomputes `num_samples` |
| Sample interval | `QComboBox` | 10 ns, 20 ns, 40 ns, 80 ns, 200 ns, 1 µs | 200 ns | Maps to `sample_interval_ns` |
| Voltage range | `QComboBox` | ±10 mV … ±10 V | ±20 mV | Same list as DAQPage `_VOLTAGE_RANGES` |
| Coupling | `QComboBox` | DC, AC | DC | |

### Trigger Controls

| Control | Widget | Range | Default |
|---|---|---|---|
| Enable | `QCheckBox` | — | Unchecked |
| Threshold | `QDoubleSpinBox` | –range to +range mV | 6.0 mV |
| Direction | `QComboBox` | Rising, Falling, Rising or Falling | Rising |
| Auto-timeout | `QSpinBox` | 0 – 10000 ms | 1000 ms |

When trigger is disabled, grey out threshold/direction/auto controls.

### Averaging Controls

| Control | Widget | Range | Default |
|---|---|---|---|
| Rate averaging window | `QSpinBox` | 1 – 120 s | 10 s |
| Trend plot window | `QSpinBox` | 10 – 300 s | 60 s |

---

## Waveform Plot — Axis Limits and Behavior

- **Y-axis:** fixed to `±voltage_range_v * 1000` mV. Do not auto-scale — the
  user needs a stable reference to judge signal amplitude.
- **X-axis:** 0 to `window_duration_ms * 1000` µs (i.e. the full trace length).
  Update bounds when window duration changes.
- **Y-axis label:** "Voltage (mV)"
- **X-axis label:** "Time (µs)"
- Enable grid: `plot.showGrid(x=True, y=True, alpha=0.3)`
- Plot pen: `style.PLOT_FG` at 1 px width

Band lines use `pyqtgraph.InfiniteLine(angle=0, ...)`. Create them once per
band and store in a dict keyed by band label; update position and color when
bands change. Remove lines for deleted bands.

---

## Important Implementation Notes

1. **`PicoScopeService` is not thread-safe.** Create one instance per page;
   do not share it with `DAQPage`. `RatemeterPage` owns its own
   `PicoScopeService` instance.

2. **`SignalExtractor.find_peaks` expects a baseline-corrected voltage array.**
   Always call `WaveformProcessor.estimate_baseline` → `subtract_baseline`
   first, then pass the corrected array along with the raw `baseline_mean` and
   `baseline_rms`.

3. **Peak amplitudes from `SignalExtractor` are already baseline-subtracted
   volts** (`amplitude_v = voltage[idx] - baseline_mean`). Multiply by 1000
   to compare against `AmplitudeBand.low_mv` / `high_mv`.

4. **Do not import from `pages/` inside `services/`.** The worker must not
   reference anything in `pages/`.

5. **Debounce spin box changes with a single-shot QTimer (300 ms)** to avoid
   triggering `_restart_worker` on every digit typed. Connect
   `valueChanged` → timer reset; timer timeout → `_restart_worker`.

6. **`QTableWidget` color cells:** store the hex string in `Qt.UserRole` on
   each color cell. On `QColorDialog` accepted, update both `UserRole` and the
   cell's background color. Read back via `item.data(Qt.UserRole)`.

7. **Band label auto-numbering:** when a row is removed, renumber all rows
   (Band 1, Band 2, …) and update `AmplitudeBand.label` accordingly. The
   worker must be restarted after renumbering so the new labels match the
   `rates_updated` dict keys.

8. **Rate display rebuilds:** when bands change, tear down and rebuild the live
   rate display widget rows entirely rather than trying to patch individual
   labels. This is simpler and avoids stale references.

9. **The trend plot must discard data for deleted bands and add new deques for
   added bands.** Clear all trend data and `PlotDataItem`s on any band list
   change, then rebuild.

10. **`autoTrigger_ms` in `PicoScopeService.set_trigger`** is currently
    hardcoded to 1000. Update that method to accept an `auto_trigger_ms: int`
    parameter (default 1000), sourced from `AcquisitionConfig` if you add it
    there, or passed directly. This ensures the ratemeter's auto-timeout
    setting actually takes effect.