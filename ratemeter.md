# Ratemeter Transit Discrimination & Recording — Implementation Spec

## Overview

Extend the ratemeter page with three related features:

1. **Signal width measurement** — measure how wide (in time) each detected peak is, so narrow "splat" contact signals can be distinguished from broader "transit" signals where a particle flies through the pickup electrode.
2. **Live transit % and velocity display** — show per-band breakdown of splat vs. transit percentage, and the average transit particle velocity, updating live alongside the existing Hz rate.
3. **Record Data button** — write every detected peak event (amplitude, width, type, velocity) to a CSV file while the user records.

### Physics background

The pickup electrode is **1.3 inches (0.03302 m)** long. When a particle transits through it, the signal width equals the time the particle spends inside — the transit time Δt. From this:

```
velocity  v = L / Δt          [m/s]
```

Kinetic energy requires mass (available from CDMS calibration later); velocity alone is derivable from width.

Narrow, tall spikes are contact/splat events (particle hits the electrode). Broad, square-wave-shaped signals are transit events. A user-configurable minimum width threshold per band distinguishes the two.

---

## Architecture rules (from CLAUDE.md — follow these strictly)

- `pages/` calls `services/` — never the reverse.
- Each `services/` module owns one concern. Do not combine concerns.
- Do **not** modify `services/serial_manager.py`, `pages/pressure_page.py`, or `services/data_recorder.py`.
- Prefer dataclasses with explicit fields over loose dicts/tuples.
- Never block the Qt UI thread — all acquisition stays in `RatemeterWorker` (`QThread`).
- Physics constants must live in config dataclasses, not hard-coded in processing logic.
- New dependencies must be minimal — `scipy.signal.peak_widths` is already an indirect dependency via `scipy.signal.find_peaks` which is in use; no new packages needed.

---

## Files to create

| File | Purpose |
|---|---|
| `services/ratemeter_logger.py` | New — append-only CSV logger for ratemeter peak events |

## Files to modify

| File | What changes |
|---|---|
| `services/daq_models.py` | Extend `PeakRecord`, `AmplitudeBand`, `RatemeterConfig`; add `RatemeterEvent` dataclass |
| `services/signal_extractor.py` | Measure peak widths using `scipy.signal.peak_widths` inside `find_peaks()` |
| `services/ratemeter_worker.py` | Classify transit vs. splat, compute velocity, emit new signals, track velocity history |
| `pages/ratemeter_page.py` | Transit Discrimination UI group, extended band table, updated rate rows, Record button |

---

## 1. `services/daq_models.py`

### 1a. Extend `PeakRecord`

Add optional width fields. The existing three fields are unchanged.

```python
@dataclass
class PeakRecord:
    """A single detected peak within a waveform trace."""

    peak_index:    int            # sample index within the trace
    time_ns:       float          # time from trace start, nanoseconds
    amplitude_v:   float          # peak height above baseline, volts

    # Width fields — populated by SignalExtractor.find_peaks(); None if measurement fails
    width_samples: Optional[float] = None   # full-width at rel_height, in samples
    width_ns:      Optional[float] = None   # transit duration in nanoseconds
    rise_ns:       Optional[float] = None   # leading-edge crossing time (ns)
    fall_ns:       Optional[float] = None   # trailing-edge crossing time (ns)
```

### 1b. Extend `AmplitudeBand`

Add the transit minimum width threshold. `None` means width discrimination is disabled for that band — the band still counts all events normally, it just won't show a % breakdown.

```python
@dataclass
class AmplitudeBand:
    label:                str
    low_mv:               float
    high_mv:              float
    color:                str
    transit_min_width_ns: Optional[float] = None
```

### 1c. Extend `RatemeterConfig`

Add electrode geometry and width measurement settings. These must not be hard-coded in the worker or extractor.

```python
@dataclass
class RatemeterConfig:
    channel:            str
    voltage_range_v:    float
    coupling:           str
    sample_interval_ns: int
    window_duration_ms: float
    rate_averaging_s:   float
    bands:              List[AmplitudeBand]
    electrode_length_m: float = 0.03302   # 1.3 inches — pickup electrode length
    width_rel_height:   float = 0.5       # fractional height for peak_widths (0.5 = FWHM)

    @property
    def num_samples(self) -> int:
        return max(1, int(self.window_duration_ms * 1e6 / self.sample_interval_ns))

    def to_acquisition_config(self, ...) -> AcquisitionConfig:
        ...  # unchanged
```

### 1d. Add `RatemeterEvent` dataclass

One instance is emitted per detected peak. Written to CSV when recording is active.

```python
@dataclass
class RatemeterEvent:
    """
    One detected peak event from the ratemeter worker.
    Emitted per peak regardless of recording state; logged to CSV when recording.
    """
    timestamp:        datetime
    band_label:       str
    amplitude_mv:     float
    width_ns:         Optional[float]   # None if width measurement failed
    event_type:       str               # "transit" | "splat" | "unknown"
    velocity_m_s:     Optional[float]   # None when width_ns is None or threshold unset
    transit_time_us:  Optional[float]   # Δt in microseconds (width_ns / 1000)
```

`event_type` rules:
- `"unknown"` — `transit_min_width_ns` is `None` for this band (discrimination off)
- `"transit"` — `width_ns >= transit_min_width_ns`
- `"splat"`   — `width_ns < transit_min_width_ns` (or `width_ns` is `None` and threshold is set)

---

## 2. `services/signal_extractor.py`

### 2a. Import `peak_widths`

```python
from scipy.signal import find_peaks, peak_widths
```

### 2b. Update `find_peaks()` signature

Add `width_rel_height` parameter. Default `0.5` (FWHM). Passed through from `RatemeterConfig`.

```python
def find_peaks(
    self,
    voltage: np.ndarray,
    baseline_mean: float,
    baseline_rms: float,
    time_ns: np.ndarray,
    height_threshold_v: Optional[float] = None,
    width_rel_height: float = 0.5,          # NEW
) -> List[PeakRecord]:
```

### 2c. Measure widths after `find_peaks`, before building `PeakRecord` list

Insert this block between the `find_peaks(...)` call and the loop that builds `PeakRecord` objects:

```python
# Width measurement — uses scipy peak_widths on the same voltage array
if len(indices) > 0:
    sample_interval_ns = float(time_ns[1] - time_ns[0]) if len(time_ns) > 1 else 1.0
    widths_samp, _, left_ips, right_ips = peak_widths(
        voltage,
        indices,
        rel_height=width_rel_height,
    )
else:
    sample_interval_ns = float(time_ns[1] - time_ns[0]) if len(time_ns) > 1 else 1.0
    widths_samp = left_ips = right_ips = np.array([])
```

### 2d. Populate width fields on each `PeakRecord`

Replace the existing loop body that builds `PeakRecord` objects:

```python
peaks = []
for i, idx in enumerate(indices):
    if i < len(widths_samp):
        w_samp  = float(widths_samp[i])
        w_ns    = w_samp * sample_interval_ns
        l_idx   = max(0, min(int(round(float(left_ips[i]))),  len(time_ns) - 1))
        r_idx   = max(0, min(int(round(float(right_ips[i]))), len(time_ns) - 1))
        rise_ns = float(time_ns[l_idx])
        fall_ns = float(time_ns[r_idx])
    else:
        w_samp = w_ns = rise_ns = fall_ns = None

    peaks.append(PeakRecord(
        peak_index    = int(idx),
        time_ns       = float(time_ns[idx]),
        amplitude_v   = float(voltage[idx]),
        width_samples = w_samp,
        width_ns      = w_ns,
        rise_ns       = rise_ns,
        fall_ns       = fall_ns,
    ))

peaks.sort(key=lambda p: p.time_ns)
return peaks
```

### 2e. Update `summarize()` — add `mean_width_ns` to returned dict

In the `summarize()` method, after computing `mean_peak_spacing_ns`, add:

```python
widths_ns = [p.width_ns for p in peaks if p.width_ns is not None]
mean_width_ns = float(np.mean(widths_ns)) if widths_ns else None
```

Add `"mean_width_ns": mean_width_ns` to the returned dict.

---

## 3. `services/ratemeter_logger.py` — new file

Create this file at `services/ratemeter_logger.py`.

```python
"""
ratemeter_logger.py
-------------------
Append-only CSV logger for ratemeter peak events.

One instance per recording session. Created by RatemeterPage when the user
clicks "Record Data"; closed when they stop recording or stop acquisition.

Output directory:
    Recorded Data/Ratemeter/
        <run_id>_events.csv

CSV columns (one row per detected peak event):
    timestamp, band, amplitude_mv, width_ns, event_type,
    transit_time_us, velocity_m_s
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from instrument_app.services.daq_models import RatemeterEvent


_CSV_FIELDS = [
    "timestamp",
    "band",
    "amplitude_mv",
    "width_ns",
    "event_type",
    "transit_time_us",
    "velocity_m_s",
]


class RatemeterLogger:
    """
    File I/O manager for one ratemeter recording session.

    Parameters
    ----------
    base_dir : Path
        Root output directory (see default_base_dir()).
    run_id : str
        Unique run identifier stamped on the filename.
    """

    def __init__(self, base_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        self._path  = base_dir / f"{run_id}_events.csv"
        base_dir.mkdir(parents=True, exist_ok=True)

        self._file   = open(self._path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=_CSV_FIELDS)
        self._writer.writeheader()
        self._file.flush()

    @staticmethod
    def make_run_id() -> str:
        """Generate a run ID from the current datetime: YYYY_MM_DD_HHmmss."""
        return datetime.now().strftime("%Y_%m_%d_%H%M%S")

    @staticmethod
    def default_base_dir() -> Path:
        """Standard output root, parallel to DAQ's Recorded Data/DAQ/."""
        return Path(__file__).parent.parent / "Recorded Data" / "Ratemeter"

    @property
    def path(self) -> Path:
        return self._path

    def save_event(self, event: RatemeterEvent) -> None:
        """Append one peak event row. Flushes immediately — no data lost on crash."""
        self._writer.writerow({
            "timestamp":       event.timestamp.isoformat(timespec="milliseconds"),
            "band":            event.band_label,
            "amplitude_mv":    f"{event.amplitude_mv:.4f}",
            "width_ns":        f"{event.width_ns:.2f}"       if event.width_ns        is not None else "",
            "event_type":      event.event_type,
            "transit_time_us": f"{event.transit_time_us:.4f}" if event.transit_time_us is not None else "",
            "velocity_m_s":    f"{event.velocity_m_s:.2f}"   if event.velocity_m_s    is not None else "",
        })
        self._file.flush()

    def close(self) -> None:
        """Flush and close. Safe to call more than once."""
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
```

---

## 4. `services/ratemeter_worker.py`

### 4a. New signals

Add two new signals alongside the existing ones:

```python
peak_event     = pyqtSignal(object)   # RatemeterEvent — one per detected peak, always emitted
```

### 4b. Update `__init__`

Add parallel deques for transit hits and recent velocities:

```python
self._transit_times:    Dict[str, deque] = {b.label: deque() for b in config.bands}
self._recent_velocities: Dict[str, deque] = {b.label: deque() for b in config.bands}
# velocity deque stores (timestamp_monotonic, velocity_m_s) tuples
```

### 4c. Update `find_peaks` call in `run()`

Pass `width_rel_height` from config:

```python
peaks = self._extractor.find_peaks(
    corrected, baseline_mean, baseline_rms, record.time_ns,
    height_threshold_v=height_override,
    width_rel_height=self._config.width_rel_height,   # NEW
)
```

### 4d. Replace the peak binning block in `run()`

Replace the existing `for band in self._config.bands` loop with:

```python
now_wall = time.monotonic()
now_dt   = datetime.now()
L        = self._config.electrode_length_m

for band in self._config.bands:
    dq         = self._hit_times[band.label]
    transit_dq = self._transit_times[band.label]
    vel_dq     = self._recent_velocities[band.label]

    for peak in peaks:
        amp_mv = peak.amplitude_v * 1000.0
        if not (band.low_mv <= amp_mv <= band.high_mv):
            continue

        dq.append(now_wall)
        w_ns = peak.width_ns

        # Classify
        if band.transit_min_width_ns is None:
            event_type   = "unknown"
            velocity     = None
            transit_us   = None
        elif w_ns is not None and w_ns >= band.transit_min_width_ns:
            event_type   = "transit"
            transit_us   = w_ns / 1000.0
            velocity     = L / (w_ns * 1e-9) if w_ns > 0 else None
            transit_dq.append(now_wall)
            if velocity is not None:
                vel_dq.append((now_wall, velocity))
        else:
            event_type   = "splat"
            transit_us   = None
            velocity     = None

        self.peak_event.emit(RatemeterEvent(
            timestamp       = now_dt,
            band_label      = band.label,
            amplitude_mv    = amp_mv,
            width_ns        = w_ns,
            event_type      = event_type,
            velocity_m_s    = velocity,
            transit_time_us = transit_us,
        ))
```

### 4e. Update `_compute_rates()`

Return a richer dict with `velocities` added. Also prune `_transit_times` and `_recent_velocities`:

```python
def _compute_rates(self, now: float) -> dict:
    cutoff  = now - self._config.rate_averaging_s
    elapsed = min(now - self._loop_start, self._config.rate_averaging_s)

    rates:      Dict[str, float]          = {}
    transits:   Dict[str, float]          = {}
    fractions:  Dict[str, Optional[float]] = {}
    velocities: Dict[str, Optional[float]] = {}

    for band in self._config.bands:
        dq = self._hit_times[band.label]
        while dq and dq[0] < cutoff:
            dq.popleft()

        tdq = self._transit_times[band.label]
        while tdq and tdq[0] < cutoff:
            tdq.popleft()

        vdq = self._recent_velocities[band.label]
        while vdq and vdq[0][0] < cutoff:
            vdq.popleft()

        n_all     = len(dq)
        n_transit = len(tdq)
        rate      = n_all     / elapsed if elapsed > 0 else 0.0
        t_rate    = n_transit / elapsed if elapsed > 0 else 0.0

        rates[band.label]     = rate
        transits[band.label]  = t_rate
        fractions[band.label] = (
            (n_transit / n_all * 100.0)
            if (n_all > 0 and band.transit_min_width_ns is not None)
            else None
        )
        velocities[band.label] = (
            sum(v for _, v in vdq) / len(vdq) if vdq else None
        )

    return {
        "rates":      rates,
        "transits":   transits,
        "fractions":  fractions,
        "velocities": velocities,
    }
```

### 4f. Add import at top of file

```python
from datetime import datetime
from instrument_app.services.daq_models import RatemeterConfig, RatemeterEvent
```

---

## 5. `pages/ratemeter_page.py`

### 5a. Add import

```python
from instrument_app.services.ratemeter_logger import RatemeterLogger
from instrument_app.services.daq_models import AmplitudeBand, RatemeterConfig, RatemeterEvent
```

### 5b. Add instance variables in `__init__`

```python
self._logger:    Optional[RatemeterLogger] = None
self._recording: bool = False
self._transit_pct_labels: Dict[str, QLabel] = {}   # band_label -> QLabel (second line)
```

### 5c. Extend the Bands table to 5 columns

Change the `QTableWidget` from 4 to 5 columns:

```python
self.table_bands = QTableWidget(0, 5)
self.table_bands.setHorizontalHeaderLabels(
    ["#", "Low (mV)", "High (mV)", "Color", "Min width (ns)"]
)
```

The 5th column is free-text: the user types a nanosecond threshold (e.g. `500`) or leaves it blank to disable discrimination for that band.

Update `_on_band_item_changed` to also trigger on column 4:
```python
if item.column() in (1, 2, 4):
    self._on_bands_changed()
```

### 5d. Update `_write_band_row`

Add the 5th column when writing a row:

```python
width_text = f"{band.transit_min_width_ns:g}" if band.transit_min_width_ns is not None else ""
self.table_bands.setItem(row, 4, QTableWidgetItem(width_text))
```

The `band` argument may currently be positional — add `transit_min_width_ns` to the signature or pass an `AmplitudeBand` directly, whichever matches the existing pattern.

### 5e. Update `_bands_from_table`

Parse the 5th column:

```python
try:
    w_text = self.table_bands.item(row, 4).text().strip()
    transit_min_width_ns = float(w_text) if w_text else None
except (AttributeError, ValueError):
    transit_min_width_ns = None

bands.append(AmplitudeBand(
    label=label, low_mv=low, high_mv=high, color=color,
    transit_min_width_ns=transit_min_width_ns,
))
```

### 5f. Add Transit Discrimination group to left panel

Add a new `QGroupBox` to the left scroll panel, inserted between the Bands group and the Run group:

```python
def _make_transit_group(self) -> QGroupBox:
    box = QGroupBox("Transit Discrimination")
    lay = QVBoxLayout(box)

    lay.addWidget(QLabel("Electrode length:  1.3 in  (33.0 mm)  [fixed]"))

    lay.addWidget(QLabel("Measure width at:"))
    self.combo_width_rel_height = QComboBox()
    self.combo_width_rel_height.addItems([
        "50%  (FWHM — default)",
        "20%  (near base)",
        "10%  (base width)",
    ])
    self.combo_width_rel_height.currentIndexChanged.connect(self._schedule_restart)
    lay.addWidget(self.combo_width_rel_height)

    note = QLabel(
        "Set a minimum width in the Bands table to enable\n"
        "transit % and velocity display for that band.\n"
        "Signals below the threshold are counted as splat."
    )
    note.setWordWrap(True)
    note.setStyleSheet(f"color: {style.TXT_DIM}; font-size: 9pt;")
    lay.addWidget(note)

    return box
```

### 5g. Add Record Data button to the Run group

In `_make_control_group()`, after the existing Stop button and a `QFrame` separator:

```python
sep = QFrame()
sep.setFrameShape(QFrame.HLine)
lay.addWidget(sep)

self.btn_record = QPushButton("⏺  Record Data")
self.btn_record.setCheckable(True)
self.btn_record.setEnabled(False)   # enabled only while acquisition is running
self.btn_record.setToolTip(
    "Write all detected peak events to a CSV file.\n"
    "Includes timestamp, band, amplitude, width, event type, and velocity."
)
self.btn_record.clicked.connect(self._on_record_toggled)
lay.addWidget(self.btn_record)

self.lbl_recording = QLabel("")
self.lbl_recording.setStyleSheet("color: #ef5350; font: bold 9pt;")
self.lbl_recording.setWordWrap(True)
lay.addWidget(self.lbl_recording)
```

### 5h. Recording state methods

```python
def _on_record_toggled(self, checked: bool) -> None:
    if checked:
        self._start_recording()
    else:
        self._stop_recording()

def _start_recording(self) -> None:
    run_id = RatemeterLogger.make_run_id()
    self._logger = RatemeterLogger(RatemeterLogger.default_base_dir(), run_id)
    self._recording = True
    self.btn_record.setText("⏹  Stop Recording")
    self.lbl_recording.setText(f"● REC  {self._logger.path.name}")

def _stop_recording(self) -> None:
    if self._logger:
        self._logger.close()
        self._logger = None
    self._recording = False
    self.btn_record.setText("⏺  Record Data")
    self.lbl_recording.setText("")
```

### 5i. Update `stop_acquisition()`

Always close the logger when acquisition stops, regardless of whether the user clicked Stop Recording:

```python
def stop_acquisition(self) -> None:
    self._stop_recording()
    self.btn_record.setChecked(False)
    self.btn_record.setEnabled(False)
    # ... existing stop logic unchanged ...
```

### 5j. Enable Record button when acquisition starts

In `_set_controls_running()` (or wherever the Start button is handled after the worker is launched):

```python
self.btn_record.setEnabled(True)
```

### 5k. Wire `peak_event` signal in `_start_worker()`

```python
self._worker.peak_event.connect(self._on_peak_event)
```

```python
def _on_peak_event(self, event: RatemeterEvent) -> None:
    """Route peak events to the logger when recording is active."""
    if self._recording and self._logger:
        self._logger.save_event(event)
```

### 5l. Update `_build_config()`

Wire in `width_rel_height` and `electrode_length_m`:

```python
_WIDTH_REL_HEIGHT_MAP = {0: 0.5, 1: 0.2, 2: 0.1}

def _build_config(self) -> RatemeterConfig:
    # ... existing fields ...
    width_rel_height = _WIDTH_REL_HEIGHT_MAP.get(
        self.combo_width_rel_height.currentIndex(), 0.5
    )
    return RatemeterConfig(
        # ... existing fields unchanged ...
        electrode_length_m = 0.03302,
        width_rel_height   = width_rel_height,
    )
```

### 5m. Update `_rebuild_rate_rows()`

Each band gets a two-line display: the existing Hz label on top, and a new dimmer label below for the transit breakdown. Replace the existing `_rebuild_rate_rows` implementation:

```python
def _rebuild_rate_rows(self, bands: List[AmplitudeBand]) -> None:
    self._clear_layout(self.rates_layout)
    self._rate_value_labels  = {}
    self._transit_pct_labels = {}

    for band in bands:
        band_widget = QWidget()
        vlay = QVBoxLayout(band_widget)
        vlay.setSpacing(2)
        vlay.setContentsMargins(0, 4, 0, 4)

        # Top row — colour swatch, band name/range, Hz readout
        top_row = QHBoxLayout()
        swatch = QLabel()
        swatch.setFixedSize(14, 14)
        swatch.setStyleSheet(f"background: {band.color}; border-radius: 3px;")
        name_lbl = QLabel(f"{band.label}   {band.low_mv:g} – {band.high_mv:g} mV")
        name_lbl.setStyleSheet(f"color: {style.TXT};")
        rate_lbl = QLabel("0.0 Hz")
        rate_lbl.setStyleSheet(
            f"color: {band.color}; font: bold 18pt 'Consolas';"
        )
        top_row.addWidget(swatch)
        top_row.addWidget(name_lbl)
        top_row.addStretch()
        top_row.addWidget(rate_lbl)
        vlay.addLayout(top_row)

        # Second row — transit % and velocity (hidden when threshold not set)
        pct_lbl = QLabel("")
        pct_lbl.setStyleSheet(
            f"color: {style.TXT_DIM}; font: 10pt 'Consolas'; padding-left: 22px;"
        )
        vlay.addWidget(pct_lbl)

        self.rates_layout.addWidget(band_widget)
        self._rate_value_labels[band.label]  = rate_lbl
        self._transit_pct_labels[band.label] = pct_lbl
```

### 5n. Update `_on_rates_updated()`

Unpack the richer payload dict and update both labels per band:

```python
def _on_rates_updated(self, payload: dict) -> None:
    rates      = payload["rates"]
    fractions  = payload["fractions"]
    velocities = payload["velocities"]

    for label, rate_lbl in self._rate_value_labels.items():
        rate_lbl.setText(f"{rates.get(label, 0.0):.1f} Hz")

        pct_lbl  = self._transit_pct_labels.get(label)
        fraction = fractions.get(label)    # None when threshold not set
        avg_vel  = velocities.get(label)   # None when no transit events yet

        if pct_lbl is None:
            continue

        if fraction is not None:
            splat_pct = 100.0 - fraction
            if avg_vel is not None:
                vel_str = (
                    f"~{avg_vel / 1000:.2f} km/s"
                    if avg_vel >= 1000
                    else f"~{avg_vel:.0f} m/s"
                )
                pct_lbl.setText(
                    f"↳  {splat_pct:.1f}% splat  ·  {fraction:.1f}% transit  ({vel_str})"
                )
            else:
                pct_lbl.setText(
                    f"↳  {splat_pct:.1f}% splat  ·  {fraction:.1f}% transit"
                )
        else:
            pct_lbl.setText("")

    # Trend plot update — unchanged from current implementation
    ...
```

### 5o. Persist new settings

In `_save_settings()` add:
```python
s.setValue("ratemeter/width_rel_height_idx", self.combo_width_rel_height.currentIndex())
```

In `_load_settings()` add:
```python
self.combo_width_rel_height.setCurrentIndex(
    s.value("ratemeter/width_rel_height_idx", 0, type=int)
)
```

For band persistence, the existing JSON bands serialisation in `_save_settings` / `_load_bands_from_json` needs to include `transit_min_width_ns`:

```python
# _save_settings bands list:
bands = [
    {
        "label":                b.label,
        "low_mv":               b.low_mv,
        "high_mv":              b.high_mv,
        "color":                b.color,
        "transit_min_width_ns": b.transit_min_width_ns,   # NEW — None serialises to null
    }
    for b in self._bands_from_table()
]

# _load_bands_from_json — when reading each entry:
transit_min_width_ns = entry.get("transit_min_width_ns")  # None if absent or null
```

---

## Expected live display

Each band's rate row will render as:

```
● Band 1   0 – 50 mV                        12.3 Hz
  ↳  31.6% splat  ·  68.4% transit  (~2.47 km/s)
```

When `transit_min_width_ns` is blank for a band, the second line is empty. When no transit events have occurred yet within the averaging window, the velocity is omitted:

```
  ↳  100.0% splat  ·  0.0% transit
```

---

## Expected CSV output

File location: `Recorded Data/Ratemeter/<run_id>_events.csv`

```
timestamp,band,amplitude_mv,width_ns,event_type,transit_time_us,velocity_m_s
2026-08-28T14:23:01.412,Band 1,18.3241,13420.50,transit,13.4205,2461.23
2026-08-28T14:23:01.419,Band 1,19.1052,,splat,,
2026-08-28T14:23:01.431,Band 1,17.8800,12980.00,transit,12.9800,2542.37
2026-08-28T14:23:01.445,Band 2,47.2100,950.00,splat,,
```

- One row per detected peak, flushed immediately after each write.
- `width_ns`, `transit_time_us`, `velocity_m_s` are empty strings when `None` (not `"None"`).
- `event_type` is `"unknown"` when no threshold is set for the band.

---

## Do not change

- `services/serial_manager.py`
- `pages/pressure_page.py`
- `services/data_recorder.py`
- `services/daq_logger.py` (the DAQ page logger — unrelated)
- `services/acquisition_worker.py` (the DAQ page worker — unrelated)
- Any DAQ page files