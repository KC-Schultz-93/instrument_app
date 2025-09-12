Architecture review and targeted improvements

Overview

- Current structure (services/ vs processing/ vs pages/) is solid and already modular.
- Two main user-facing tabs exist:
  - pages/cdms_page.py as the operator station (AO/DO, acquisition, basic event triage)
  - pages/processing_page.py as the analysis dashboard (calibration, m/z histograms)
- services/scope_pico.py handles Pico Rapid mode cleanly with Qt signals.
- processing/geo_calibration.py centralizes physics and per-frame FFT+peak logic.

Pain points and opportunities

- Wiring duplication: Both pages set up their own threads and connect source -> analyzer/worker directly. This repeats lifecycle logic and makes it harder to add a second consumer later.
- Event/result types: cdms_page defines its own EventResult and inline Analyzer; processing defines Ion in geo_calibration. We can tighten these contracts so producers/consumers agree on a couple of shared types.
- Streaming histograms: NumPy hist updates work, but scale better with a columnar store when ion rates increase or when we want snapshots/queries. Polars fits this nicely.
- Single-scope ownership: Multiple places can instantiate a Pico service today. Centralizing ownership simplifies coordination and avoids accidental double-opens.

Additions in this change set

- core/bus.py: A lightweight Qt signal bus (frame_block, ions_batch, status). Producers and consumers can connect without hard references to each other. Enables multi-subscriber graphs.
- processing/aggregators.py: PolarsHistogram for scalable, incremental histograms (with a NumPy fallback). Accepts List[Ion] and returns edges/counts for plotting.
- services/sources.py: Unified SyntheticSource, PicoSource, and a SourceManager to control a single active source and publish frames on the bus. Pages can subscribe to the bus and no longer need to own the scope lifecycle directly.
- requirements.txt: Promote polars for analysis; keep pyarrow/duckdb as optional.

Recommended next steps (incremental, low-risk)

1) Centralize frame routing via the bus
   - Hook services/sources.SourceManager into app startup (or into a small shared AppContext module) and let pages subscribe to core.bus.bus.frame_block.
   - Keep the existing page-local SyntheticGenerator/Analyzer working until the bus path is validated; then remove duplication.

2) Introduce a shared Processor interface
   - Extract a thin processing/base.py that wraps RealTimeCDMS from geo_calibration and exposes process_block(x, fs) -> List[Ion].
   - Add a small worker that subscribes to bus.frame_block, runs process_block, then emits bus.ions_batch. This way any page can visualize or record ions.

3) Adopt PolarsHistogram in processing_page
   - Replace the NumPy histogram path with processing/aggregators.PolarsHistogram. Keep bin count/bin width configurable in the UI.
   - Provide a user toggle to switch field (mz vs mass) and range, and use Polars to recompute quickly.

4) Clarify result/data types
   - Move EventResult (cdms_page) to a shared core/types.py alongside a light IonView type (or reuse processing.Ion). This removes duplication and makes buses strongly typed at the edges.

5) Throughput and backpressure
   - If needed, add a small ring-buffer or a "latest only" policy for worker input to avoid backlog under bursty load.
   - Consider making the processing worker use a QThreadPool (QRunnable) when parallelizing heavy routines in the future.

Notes on Polars use

- For light rates, NumPy remains fine. Polars starts to shine when ion counts become large or when downstream exports/queries are needed (e.g., cut-by-range, per-ion snapshots, parquet export).
- The PolarsHistogram maintains an append-only column for the target field, and computes bins via a bin-index group_by, which is efficient and robust.

Compatibility and risk

- No existing behavior is removed by this patch. The new modules are additive.
- Migration can be done page-by-page: first route frames via bus, then plug in a processor-on-bus, finally switch histogram over to Polars.

