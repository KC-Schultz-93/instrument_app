Architecture

Overview

- Two main user-facing tabs exist:
  - pages/pressure_page.py: pressure/interlock monitoring and control, via services/serial_manager.py.
  - pages/daq_page.py: PicoScope 4262 waveform acquisition and CDMS analysis.
- The DAQ pipeline runs synchronously inside services/acquisition_worker.py (a QThread), calling
  services/picoscope_service.py -> services/event_detector.py -> services/signal_extractor.py ->
  services/cdms_analyzer.py -> services/daq_logger.py in sequence per trace. No shared event bus;
  the worker emits results via services/daq_channels.py (a DAQ-only Qt signal bus) back to daq_page.py.
- DAQ and pressure/serial are intentionally decoupled: neither imports the other's services, and
  daq_channels.py carries no serial/pressure signals.

History note

- An earlier, independent PicoScope/CDMS pipeline was built directly on this branch: core/bus.py
  (a general Qt signal bus), services/scope_pico.py (ps4000a driver), services/sources.py,
  processing/processor_worker.py, processing/geo_calibration.py, processing/aggregators.py,
  pages/cdms_page.py, and pages/processing_page.py. It was developed in parallel with, and
  independently of, the DAQ pipeline built on the nano_daq branch (ps4000 driver, worker-thread
  model, no shared bus). When the two branches were merged, the nano_daq pipeline was kept as the
  one going forward (it matches the current CLAUDE.md spec and hardware driver), and the bus-based
  pipeline and its exclusive dependents were removed rather than run in parallel.
- If old commit history, comments, or docs reference core/bus.py, services/scope_pico.py,
  services/sources.py, processing/*, pages/cdms_page.py, or pages/processing_page.py, they're
  describing the removed pipeline.

Adding to the DAQ pipeline

- Each services/ module owns one concern (see CLAUDE.md's module ownership table). Extend a
  module in place rather than adding a new bus/signal path — the worker-thread + direct-call
  model is deliberate, not a placeholder for a future event bus.
