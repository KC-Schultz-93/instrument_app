# CLAUDE.md

## Purpose

This repository contains a Python instrument application for a lab setup. The next major task is to add a **DAQ (data acquisition) pipeline** for a **PicoScope 4262** while keeping the project in **Python**.

The DAQ work should use the **PicoSDK Python wrapper path** around the appropriate Pico driver family so the application can continue to use Python for the GUI, processing, logging, and orchestration layers while still communicating with the Pico hardware.

For this scope model, the target driver family is **`ps4000`**.

---

## High-level development goal

Add a DAQ subsystem that can:

1. connect to the PicoScope 4262
2. configure waveform acquisition in Python
3. acquire repeated waveforms in block mode first
4. pass acquired traces into event detection and signal extraction
5. save raw waveforms plus reduced event data for later CDMS analysis
6. integrate cleanly into the existing application structure without breaking the current serial / pressure monitoring functionality

This DAQ subsystem is for **waveform acquisition and downstream analysis only**.

At the current stage, **do not assume the program controls instrument electrodes**. Electrode control is out of scope for now.

---

## Existing repository structure

Current project structure appears to be:

```text
App/
  __init__.py
  chart_update.log
  crash_log.txt
  data_received.log
  main.py
  requirements.txt
  serial_debug.log
  settings.py

INT_SYS/

Pages/
  __init__.py
  maintenance_page.py
  pressure_page.py

Recorded Data/
  Recorded Pressures/
    2026/

Services/
  __init__.py
  Channels.py
  CustomWidgets.py
  PressureLogger.py
  SerialComms.py

Tests/
  quick_check.py
  test_graph_display.py
  test_serial.py

UI/
  __init__.py
  theme.py
```

This structure already suggests a modular pattern:
- `App/` for entrypoint and app-level settings
- `Pages/` for GUI pages
- `Services/` for hardware / data / reusable logic
- `Tests/` for validation scripts
- `UI/` for theme and presentation concerns

Follow that pattern when adding DAQ code.

---

## Required architectural direction

The DAQ code should be added in a way that preserves separation of concerns.

### Keep these concerns separate

- **hardware communication** with the PicoScope
- **waveform preprocessing**
- **event detection**
- **signal extraction / feature extraction**
- **data logging / file writing**
- **GUI page behavior**

Do **not** put all DAQ logic into a single page file or a single monolithic script.

---

## Recommended new modules

Add new files in a style consistent with the current repo.

Suggested additions:

```text
Services/
  PicoScopeService.py
  WaveformProcessor.py
  EventDetector.py
  SignalExtractor.py
  DAQLogger.py
  DAQModels.py

Pages/
  daq_page.py

Tests/
  test_pico_connection.py
  test_waveform_processing.py
  test_event_detector.py
```

### Purpose of each module

#### `Services/PicoScopeService.py`
Owns direct PicoScope interaction.

Responsibilities:
- connect / disconnect from PicoScope 4262
- configure channel(s)
- configure timebase / sample interval / record length
- configure trigger settings if used
- acquire one block waveform
- acquire repeated block waveforms
- return waveforms in a Python-friendly structure
- surface hardware errors cleanly

This should be the only place that directly talks to the Pico SDK wrapper.

#### `Services/DAQModels.py`
Owns structured data containers.

Should define lightweight models or dataclasses such as:
- `AcquisitionConfig`
- `WaveformRecord`
- `EventSummary`
- optionally `PeakRecord`

Use these to keep interfaces clear between modules.

#### `Services/WaveformProcessor.py`
Owns low-level signal conditioning.

Responsibilities:
- baseline estimation
- DC offset subtraction
- optional inversion if signal polarity needs flipping
- optional light smoothing / filtering
- noise RMS estimation
- basic trace quality checks

#### `Services/EventDetector.py`
Owns event detection logic.

Responsibilities:
- determine whether a trace contains likely ion signal
- reject empty traces
- reject obvious transient-only traces
- classify traces into categories such as:
  - empty
  - possible_event
  - likely_trapped_ion
  - noisy_trace
  - overload / clipped
  - ambiguous

Use rule-based logic first. Keep it simple and inspectable.

#### `Services/SignalExtractor.py`
Owns signal feature extraction.

Responsibilities:
- peak finding
- peak time extraction
- peak amplitude extraction
- spacing calculation
- grouping peaks into likely repeated structures
- summary statistics for accepted traces

#### `Services/DAQLogger.py`
Owns DAQ data products.

Responsibilities:
- save raw waveform data
- save reduced event summary rows
- save run metadata
- save DAQ log output

Should support later re-analysis.

#### `Pages/daq_page.py`
Owns the DAQ GUI.

Responsibilities:
- connect/disconnect controls
- acquisition settings controls
- start / stop acquisition
- live waveform display
- display accepted / rejected event counts
- display extracted feature summaries

The GUI page should call service-layer code rather than implement analysis logic directly.

---

## Required DAQ scope for this stage

The DAQ portion should focus only on these four functional areas:

1. **waveform acquisition**
2. **event detection**
3. **signal extraction**
4. **data products for later analysis**

Again, do **not** include electrode control yet.

---

## Functional requirements

### 1. Waveform acquisition

The DAQ should be able to:
- connect to the PicoScope 4262 from Python
- configure one input channel first
- perform block captures
- repeat captures in a loop without freezing the GUI
- convert raw ADC values to volts
- attach timestamps and acquisition metadata to each trace

The first implementation should prioritize **block mode**, not streaming mode.

Why:
- block mode is simpler
- easier to debug
- enough for initial CDMS waveform handling
- lower integration complexity

### 2. Event detection

The first event detection implementation should include:
- baseline mean estimation
- baseline RMS / noise estimate
- threshold-based signal presence test
- detection of multiple peaks or repeated structure
- trace classification label

Keep the logic explicit and inspectable.

### 3. Signal extraction

The first extraction implementation should include:
- peak finding
- peak times
- peak amplitudes
- peak spacing estimates
- summary statistics for each trace

Do not overcomplicate the first version with highly specialized fitting unless needed.

### 4. Data products for later analysis

The DAQ should save both:

#### Raw waveform data
For each trace, save:
- trace ID
- timestamp
- time array or sample interval
- voltage array
- acquisition metadata
- optional event classification

#### Reduced event data
For each processed trace, save a reduced summary row containing fields like:
- trace ID
- timestamp
- classification
- accepted/rejected flag
- baseline mean
- baseline RMS
- signal max
- signal min
- number of peaks
- mean peak height
- mean peak spacing
- notes

This reduced dataset should make later filtering and re-analysis easier.

---

## Expected CDMS-oriented analysis behavior

The software should be written with CDMS-style processing in mind.

The eventual analysis path is expected to resemble:

1. acquire transient waveform
2. estimate baseline / noise
3. determine whether structured ion-like signal exists
4. identify induced-signal peaks
5. measure peak timing and amplitude
6. estimate repeated spacing / periodic structure
7. save raw waveform and reduced feature summary

Later physics calculations such as charge, velocity, `m/z`, or mass can be added after the waveform pipeline is working.

Do not hard-code too much instrument-specific physics into the first DAQ layer. Keep the acquisition and feature extraction reusable.

---

## Integration guidance for this repository

The current project already includes pressure logging and serial communications. The new DAQ subsystem should coexist with those features.

### Integration rules

- Do not break `Services/SerialComms.py` or pressure-related functionality.
- Do not tightly couple PicoScope code to pressure logging code.
- Reuse the project’s existing style and naming patterns where reasonable.
- Keep new dependencies minimal.
- Prefer service-layer classes over script-style globals.
- Preserve readability for a research codebase that may be maintained by non-software specialists.

---

## Data model expectations

The DAQ code should use explicit structured records rather than passing around loose tuples.

Suggested Python dataclasses:

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import numpy as np

@dataclass
class AcquisitionConfig:
    channel: str
    voltage_range_v: float
    coupling: str
    sample_interval_s: float
    num_samples: int
    trigger_enabled: bool = False
    trigger_level_v: Optional[float] = None

@dataclass
class WaveformRecord:
    trace_id: int
    timestamp: datetime
    voltage: np.ndarray
    time: np.ndarray
    sample_interval_s: float
    metadata: dict = field(default_factory=dict)

@dataclass
class EventSummary:
    trace_id: int
    timestamp: datetime
    accepted: bool
    classification: str
    baseline_mean: float
    baseline_rms: float
    signal_max: float
    signal_min: float
    num_peaks: int
    mean_peak_height: Optional[float] = None
    mean_peak_spacing_s: Optional[float] = None
    notes: str = ""
```

Claude may refine these, but should preserve the idea of clear interfaces between acquisition, processing, and logging.

---

## Recommended file output layout

Add a DAQ-oriented storage structure similar in spirit to the current recorded pressure folders.

Suggested layout:

```text
Recorded Data/
  DAQ/
    raw/
      <run_id>/
        trace_000001.npz
        trace_000002.npz
    reduced/
      <run_id>_events.parquet
    metadata/
      <run_id>.json
    logs/
      <run_id>.log
```

Preferred file types:
- raw traces: `.npz`
- reduced tables: `.parquet` or `.csv`
- run metadata: `.json`
- logs: `.log`

If dependency simplicity matters, CSV is acceptable initially for reduced data.

---

## GUI expectations

A first DAQ page should include:
- connect/disconnect button
- status indicator for PicoScope connection
- acquisition settings controls
- start / stop acquisition controls
- waveform plot
- event counters
- text area or panel for latest event summary

Keep the first version practical rather than visually elaborate.

The GUI must remain responsive during acquisition.
Use a worker thread, timer-driven polling loop, or another safe Qt-compatible pattern instead of blocking the UI thread.

---

## Testing expectations

Add focused tests and bench scripts.

Priority tests:
- PicoScope connection test
- one-shot block acquisition test
- waveform baseline subtraction test
- peak detection test with synthetic data
- event detection classification test with synthetic traces

Where hardware is unavailable, use synthetic waveforms and mocks.

---

## Implementation priorities

Claude should work in this order unless there is a strong reason not to:

### Phase 1: minimal hardware proof-of-life
- create PicoScope service
- connect to PicoScope 4262
- acquire one block trace
- convert to volts
- display or print basic trace info

### Phase 2: basic DAQ loop
- repeated block acquisition
- waveform plot integration
- save raw traces
- add metadata logging

### Phase 3: first analysis layer
- baseline correction
- noise estimation
- peak finding
- trace classification
- reduced event summaries

### Phase 4: quality improvements
- better peak grouping
- improved classification rules
- refined GUI integration
- replay saved traces through analysis pipeline

---

## Important constraints

- Stay in **Python** for the application layer.
- Use the **Pico wrapper approach** for communicating with the PicoScope rather than switching the whole project to C or C#.
- The target scope is a **PicoScope 4262** using the **`ps4000`** driver family.
- Do not introduce unnecessary architectural complexity too early.
- Do not bundle acquisition, processing, logging, and GUI code into one file.
- Prioritize clarity, modularity, and later re-analysis of saved data.

---

## What a good first deliverable looks like

A good first incremental deliverable would be:

1. a new `PicoScopeService` that can connect to the 4262 and return one waveform
2. a lightweight DAQ page that can trigger one acquisition
3. waveform saving to disk with metadata
4. a basic event detector that computes baseline, RMS, and peak count
5. a reduced event summary written alongside the raw waveform

This is enough to validate the Pico integration and create a foundation for CDMS-specific signal analysis.

---

## Developer mindset for this repo

This is research instrumentation software, not just a generic demo app.

Prioritize:
- readable code
- explicit assumptions
- clear metadata
- recoverable raw data
- modular analysis layers
- testable signal-processing functions

Prefer code that future lab members can understand and modify.
