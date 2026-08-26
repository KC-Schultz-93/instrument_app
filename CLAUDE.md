# CLAUDE.md

## Project overview

Python instrument control application for a lab setup, targeting CDMS (Charge Detection Mass Spectrometry) waveform analysis. Mixed codebase: Python application layer + Arduino C++ firmware (`INT_SYS/`).

**Current focus: adding a DAQ pipeline for a PicoScope 4262 using the PicoSDK Python wrapper (`ps4000` driver family).**

---

## Launch command

```bash
python App/main.py
```

---

## Repository structure

```
App/          Entry point (main.py) and app-level settings
INT_SYS/      Arduino C++ firmware — see INT_SYS/CLAUDE.md
Pages/        GUI pages (one file per page)
Services/     Hardware drivers, data processing, reusable logic
Tests/        Validation scripts and bench tests
UI/           Theme and shared presentation components
Recorded Data/
  Pressures/  Existing pressure logs
  DAQ/        New — raw traces (.npz), reduced events (.parquet), metadata (.json), logs
```

---

## Architecture rules

- `Pages/` calls `Services/` — never the reverse.
- Each `Services/` module owns one concern (see below). Do not combine them.
- **Do not modify `Services/SerialComms.py` or pressure-related functionality.**
- DAQ and serial/pressure subsystems must remain decoupled.
- Prefer dataclasses with explicit fields over loose tuples or dicts.
- Use a worker thread or timer-driven polling for acquisition — never block the Qt UI thread.

---

## Service module ownership

| Module | Owns |
|---|---|
| `PicoScopeService.py` | All PicoSDK calls — only place that touches the hardware driver |
| `DAQModels.py` | Dataclasses: `AcquisitionConfig`, `WaveformRecord`, `EventSummary` |
| `WaveformProcessor.py` | Baseline estimation, DC offset, noise RMS, trace quality |
| `EventDetector.py` | Trace classification: `empty`, `possible_event`, `likely_trapped_ion`, `noisy_trace`, `overload`, `ambiguous` |
| `SignalExtractor.py` | Peak finding, timing, amplitude, spacing |
| `DAQLogger.py` | Saving raw traces, reduced summaries, run metadata, logs |
| `SerialComms.py` | Serial comms to Arduino — **do not touch** |
| `PressureLogger.py` | Pressure logging — **do not touch** |

---

## DAQ scope (current phase)

In scope: waveform acquisition → event detection → signal extraction → data products.  
**Out of scope: electrode control.** Do not add it.

Driver: `ps4000`. Block mode only for now (not streaming).

---

## Data products

Raw traces saved per-trace as `.npz` with voltage array, time array, timestamp, and metadata.  
Reduced summaries saved per-run as `.parquet` (or `.csv` if simpler) with one row per trace.

---

## Implementation order

1. `PicoScopeService` — connect, acquire one block trace, return volts
2. `daq_page.py` — connect/disconnect, trigger one acquisition, show waveform
3. `DAQLogger` — save raw trace + metadata
4. `EventDetector` — baseline, RMS, peak count
5. Reduced event summary alongside raw trace

---

## Key constraints

- Stay in Python for the application layer.
- Use `ps4000` PicoSDK wrapper — do not rewrite in C or C#.
- Keep new dependencies minimal.
- Write for readability: future lab members may not be software specialists.
- Do not hard-code physics constants (charge, m/z) into the first DAQ layer.
