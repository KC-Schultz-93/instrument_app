# Ratemeter page UI

Implemented in `pages/ratemeter_page.py`; this supersedes the collapsible left panel described in `rate_record.md`.
The original refactor spec is `ratemeter_ui_refactor.md`.

## Layout

- **Status block** (top-left): `lbl_status` (coloured idle / running / error) and `lbl_trace_count`.
- **Top bar**: Scope, Trigger and Captures tiles; Connect and Run toggle switches (`sw_connect`, `sw_run`);
  `lbl_connection` sits under the Connect switch.
- **Left rail**: Channel, Detection, Rates tiles; Data Recorder pinned at the bottom.
- **Dock**: click a tile to open its panel, click the same tile to close it, click another tile to swap. Closed on launch.
- **Plots**: waveform and rate trend. **Bottom strip**: band table + Add/Remove, live band readouts.

## Tiles

| Tile | Panel widgets | −/+ |
|---|---|---|
| Channel | channel, probe, range, coupling, bandwidth limit | range index |
| Scope | window duration, sample interval | window, 1-2-5 sequence |
| Trigger | enable, threshold, direction, auto-timeout | threshold, 10 % of true range |
| Captures | captures per batch | ±1 |
| Detection | detection mode + matched-filter controls | – |
| Rates | rate averaging, trend window, width-at-height | – |

Scope also shows derived *Samples* and *Sample rate* (`_derived_scope_values()`, equal to
`RatemeterConfig.num_samples`). A ⚠ badge on Scope/Detection means the matched-filter window warning is active.

## Behaviour notes

- Control widgets and `ratemeter/*` QSettings keys are unchanged; `section_expanded_*` keys are no longer read or written.
- Connect/Run switches use `clicked` and are re-synced from real state by `_sync_switches()` after every handler,
  so failures and "PicoScope busy" snap them back.
- `_load_settings()` sets `_loading_settings` so widget signals fired during load do not overwrite unread keys.
- New reusable widgets live in `ui/`: `ToggleSwitch`, `ParamTile`, `DockHost`, `CardFrame`.

## Tests

`tests/test_ratemeter_page_ui.py` (config/settings characterization, tiles, dock, switches) and
`tests/test_ui_tiles_smoke.py` (widgets across all themes). Run with `python -m pytest tests/`.
