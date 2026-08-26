# CLAUDE.md

## Project overview

Python instrument control application for a lab setup, targeting CDMS (Charge Detection Mass Spectrometry) waveform analysis. Mixed codebase: Python application layer (installable `instrument_app` package) + Arduino C++ firmware (`INT_SYS/`).

**Current focus: the DAQ pipeline for a PicoScope 4262 using the PicoSDK Python wrapper (`ps4000` driver family) is in place; ongoing work extends it.**

---

## Launch command

```bash
python -m instrument_app
# or
run.bat / run.ps1
```

---

## Repository structure

```
app/          Entry point (main.py), settings dialog
config/       App-level settings persistence
core/         Shared lightweight types (core/types.py)
pages/        GUI pages (one file per page)
services/     Hardware drivers, data processing, reusable logic
theme/        Theme manager, palettes, shared style tokens
ui/           Reusable themed widgets (buttons, cards, plots)
tests/        Validation scripts and bench tests
INT_SYS/      Arduino firmware for the interlock/pump/gauge controller
Recorded Data/
  Pressures/  Existing pressure logs
  DAQ/        Raw traces (.npz), reduced events (.csv), metadata (.json), logs
```

---

## Architecture rules

- `pages/` calls `services/` — never the reverse.
- Each `services/` module owns one concern (see below). Do not combine them.
- **Do not modify `services/serial_manager.py` or pressure-related functionality** (`pages/pressure_page.py`, `services/data_recorder.py`).
- DAQ and serial/pressure subsystems must remain decoupled — the DAQ page uses its own `DAQChannels` signal bus (`services/daq_channels.py`), not the serial/pressure code path.
- Prefer dataclasses with explicit fields over loose tuples or dicts.
- Use a worker thread (`QThread`) for acquisition — never block the Qt UI thread.

---

## Service module ownership

| Module | Owns |
|---|---|
| `services/picoscope_service.py` | All PicoSDK calls — only place that touches the hardware driver |
| `services/daq_models.py` | Dataclasses: `AcquisitionConfig`, `WaveformRecord`, `EventSummary`, `CDMSConfig`, `CDMSResult`, `STFTResult` |
| `services/waveform_processor.py` | Baseline estimation, DC offset, noise RMS, STFT, trace quality |
| `services/event_detector.py` | Trace classification: `empty`, `possible_event`, `likely_trapped_ion`, `noisy_trace`, `overload`, `ambiguous` |
| `services/signal_extractor.py` | Peak finding, timing, amplitude, spacing |
| `services/cdms_analyzer.py` | CDMS physics: charge, m/z, mass from calibration constants |
| `services/acquisition_worker.py` | `QThread` worker wiring PicoScope → detection → extraction → physics → logging |
| `services/daq_logger.py` | Saving raw traces, reduced summaries, run metadata, mass histograms |
| `services/daq_channels.py` | Qt signal bus for the DAQ page/worker — DAQ-only, not shared with serial/pressure |
| `services/serial_manager.py` | Serial comms to Arduino — **do not touch** |
| `services/data_recorder.py` | Pressure logging — **do not touch** |

---

## DAQ scope (current phase)

In scope: waveform acquisition → event detection → signal extraction → CDMS physics → data products.
**Out of scope: electrode control.** Do not add it.

Driver: `ps4000`. Block mode only for now (not streaming).

CDMS physics calibration (see `services/cdms_analyzer.py` / `services/daq_models.CDMSConfig`):
- Charge: `Q [e] = V_peak [µV] / charge_cal_uv_per_e` (CoolFET amplifier, default 0.64 µV/e)
- m/z: `m/z [Da/e] = K_trap [Da·Hz²] / f [Hz]²` — `K_trap = 0` means uncalibrated (returns `None`)
- Mass: `mass [Da] = charge_e × (m/z [Da/e])`

---

## Data products

Raw traces saved per-trace as `.npz` with voltage array, time array, timestamp, and metadata.
Reduced summaries saved per-run as `.csv` with one row per trace, plus a mass histogram.

---

## History note

An earlier, independent PicoScope/CDMS pipeline (`ps4000a` driver, Qt-signal `core/bus.py`, `pages/cdms_page.py` + `pages/processing_page.py`, `services/scope_pico.py`, `processing/`) was built in parallel on `main` before this DAQ pipeline was merged in from the `nano_daq` branch. That earlier pipeline was removed in favor of this one — if you see references to it in old history/docs, they're stale.

---

## Key constraints

- Stay in Python for the application layer.
- Use `ps4000` PicoSDK wrapper — do not rewrite in C or C#.
- Keep new dependencies minimal.
- Write for readability: future lab members may not be software specialists.
- Do not hard-code physics constants (charge, m/z) into the acquisition/detection layers — keep them in `CDMSConfig`.
