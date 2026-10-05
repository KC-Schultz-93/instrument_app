# Ratemeter Page — UI Restructure Plan (PicoScope 7-style tiles + docked option panels)

Handoff for Claude Code. Target: `pages/ratemeter_page.py` (the "Ratemeter" tab).
Reference screenshots (in the Nano Instrument App project): `Current_app.png` (today), `Pico_example.png`,
`Channel_select.png`, `Probe_Selection.png`, `Scope_timebase.png` (PicoScope 7 behavior to imitate),
`RM_Design_example.png` (the target layout).

---

## 1. Goal

Replace the tall left panel of always-visible controls with compact **tiles** that show the *current* value of each
setting. Clicking a tile opens a **docked option panel** with that setting's controls; clicking the same tile again
closes it. This frees the screen for the waveform and rate trend.

**This is a UI-only refactor.** Acquisition, detection, and recording logic must behave identically.

## 2. Hard constraints (read first)

1. **Do not edit** `services/*`, `core/*`, `pages/daq_page.py`, `pages/pressure_page.py`, `pages/maintenance_dialog.py`,
   or anything serial/pressure related (see `docs/CLAUDE.md`). `RatemeterWorker`, `RatemeterConfig`, `MatchedFilterConfig`,
   `PicoScopeService` are untouched.
2. **Keep every existing control widget and its attribute name**: `combo_channel`, `spin_window`, `combo_interval`,
   `combo_probe`, `combo_range`, `combo_coupling`, `chk_bandwidth_limit`, `spin_captures_per_batch`, `chk_trigger_enable`,
   `spin_trigger_threshold`, `combo_trigger_direction`, `spin_trigger_auto`, `combo_detection_mode`, all `*_mf_*` widgets,
   `spin_rate_avg`, `spin_trend_window`, `combo_width_rel_height`, `table_bands`, `btn_record`, `btn_timed_record`,
   `spin_timed_duration`, `lbl_status`, `lbl_trace_count`, `lbl_connection`, `lbl_recording`, `lbl_timed_recording`.
   Strategy: **container swap, not rewrite**. The existing `_make_*_group()` builders keep creating the same widgets and
   wiring the same signals; they just return a plain panel widget instead of a `CollapsibleBox`, and that panel is
   parented into the dock instead of the left column.
3. **Keep every `ratemeter/*` QSettings key and its meaning** (org `JohnsonLab`, app `NanoInstrumentApp`). Saved values
   must load into the new UI exactly as before. `_build_config()`, `_build_mf_config()`, `_start_worker()`,
   `_restart_worker()`, `_schedule_restart()` (300 ms debounce), `_on_rates_updated()`, and the band-table functions keep
   their logic. Only the section-expanded keys (`ratemeter/section_expanded_*`) become unused — leave them in QSettings
   untouched and stop reading/writing them.
4. Follow `docs/CLAUDE.md`: pages call services, no new dependencies, readable code, no blocking the UI thread.
5. **Theming**: use theme tokens, never hard-coded colors. Existing pattern is `ThemedMixin` + `apply_theme(t: Theme)`
   (`ui/mixins.py`, `ui/primitives.py`). Note `ThemedMixin.__init__` does *not* apply the theme — call
   `self.apply_theme(theme_mgr.current)` at the end of each new widget's `__init__` so it is styled on first show. The app-wide
   QSS in `app/main.py` sets `QWidget { background: <gradient> }`, so every new container needs an explicit background
   (see how `CollapsibleBox.apply_theme` styles `content`). Test on all themes in `theme/themes.py`
   (Dark, Light, Submarine, Neon Lights, Chroma Glow, Ember, Violet, Hazard).

## 3. Target layout

```
┌──────────────┬──────────────────────────────────────────────────────┬────────────────────────┐
│ STATUS block │ [Scope tile] [Trigger tile] [Captures tile]          │  Connect ◯──  Run ◯──  │  ← top bar
│ Idle/Running │   −  5.00 ms  +   −  6 mV ↑  +   −  1 / batch +      │   (toggle switches)    │
│ Traces: 0    │   Samples 25 kS · Rate 5 MS/s                        │                        │
├──────────────┼──────────┬───────────────────────────────────────────┴────────────────────────┤
│ [Channel A]  │  DOCK    │  Waveform plot                                                      │
│  DC x1 ±20mV │  (one    │                                                                     │
│ [Detection]  │  panel   ├─────────────────────────────────────────────────────────────────────┤
│  Matched flt │  open at │  Rate Trend plot                                                    │
│ [Rates]      │  a time; ├───────────────────────────────┬─────────────────────────────────────┤
│  10 s / 60 s │  hidden  │  Band table (editable, live)  │  Band readouts (swatch, range, Hz,  │
│              │  when    │  [+ Add] [- Remove]           │  transit % / velocity line)         │
│ ── Recorder ─│  closed) │                               │                                     │
│ (unchanged)  │          │                               │                                     │
└──────────────┴──────────┴───────────────────────────────┴─────────────────────────────────────┘
```

- **Top-left status block** (like PicoScope's green "Running" box): shows `lbl_status` text with a state color
  (idle/running/error) plus `lbl_trace_count`. Keep both QLabel objects — `_on_status`, `_on_error`, `_on_trace_count`
  keep writing to them.
- **Top-right toggle switches, always visible**: **Connect** and **Run** (see §5).
- **Left rail**: Channel, Detection, Rates tiles, then the **Data Recorder pinned at the bottom-left with its current
  internal arrangement unchanged** (Record Data button, `lbl_recording`, separator, timed-duration spin, Timed Recording
  button, `lbl_timed_recording`). Extract just the recorder half of `_make_control_group()` into
  `_make_recorder_group()`; the Start/Stop/status half is replaced by the top bar.
- **Dock**: sits between the rail and the plots, ~320 px wide, scrollable inside, pushes the plots right when open
  (not an overlay). **One panel open at a time** (as in PicoScope): clicking another tile swaps the panel; clicking the open
  tile's header closes it. Closed by default on launch.
- **Bottom strip**: band table (`table_bands`, always editable, including while running, + Add / − Remove) bottom-left of the
  plot area; the live band readouts (today's `rates_frame` / `_rebuild_rate_rows`) bottom-right. Band count is dynamic.
  The rate-readout frame moves out of the vertical splitter between waveform and trend into this strip. Keep
  `_rebuild_rate_rows`, `_rate_value_labels`, `_transit_pct_labels` logic as is.
- Waveform and Rate Trend stay in a vertical `QSplitter`.

## 4. Tile → panel mapping

| Tile (location) | Shows on the tile | Panel contents (existing widgets, moved) | −/+ buttons step |
|---|---|---|---|
| **Channel** (rail) | channel letter, coupling, probe, true range, e.g. `A  DC  x1  ±20 mV`; `BW` badge if limit on | `combo_channel`, `combo_probe`, `combo_range`, `combo_coupling`, `chk_bandwidth_limit` | `combo_range` index |
| **Scope** (top bar) | window duration + **derived** `Samples` and `Sample rate` (see §6) | `spin_window`, `combo_interval` | `spin_window` through a 1-2-5 sequence |
| **Trigger** (top bar) | `Off`, or `6.0 mV ↑/↓/⇅` | `chk_trigger_enable`, `spin_trigger_threshold`, `combo_trigger_direction`, `spin_trigger_auto` | `spin_trigger_threshold` (step = 10% of true range) |
| **Captures** (top bar) | `N / batch` | `spin_captures_per_batch` (can live in the Scope panel too — see Open items) | `spin_captures_per_batch` ±1 |
| **Detection** (rail) | `Simple threshold` or `Matched filter · 85 µs · 0.35` | `combo_detection_mode` + `_mf_controls` (all matched-filter widgets, preview/warning labels, template loader) | none |
| **Rates** (rail) | `10 s avg · 60 s trend · FWHM` | `spin_rate_avg`, `spin_trend_window`, `combo_width_rel_height` + its note (the old Averaging + Transit groups) | none |

If `lbl_mf_window_warning` has text, show a small ⚠ badge on the Detection tile and the Scope tile.

## 5. Connect / Run toggle switches

New `ToggleSwitch` (checkable `QAbstractButton`, painted track + knob, themed). Replace `btn_connect`/`btn_disconnect`
and `btn_start`/`btn_stop`; **reuse the existing handlers**:

- Connect switch `clicked` → `_on_connect_clicked` if now ON, else `_on_disconnect_clicked`.
- Run switch `clicked` → `_on_start_clicked` if now ON, else `_on_stop_clicked`.
- Use `clicked`, not `toggled`, so programmatic state sync never re-triggers a handler.
- After every handler call, and at the end of `_set_controls_idle()` / `_set_controls_running()` / `_on_daq_busy()`, run
  a `_sync_switches()` that sets each switch from **real state** (`self._service.is_connected`, `self._worker is not None`)
  with `blockSignals(True/False)`. This is what makes failure paths correct: Connect failing, "PicoScope Busy"
  warning in `_on_start_clicked`, hardware config error — the switch must snap back to OFF.
- Preserve today's enable rules: Connect switch disabled while running; Run switch enabled when
  (connected and not `_daq_busy`) or currently running; Record / Timed Record enabled only while running.
- Keep `stop_acquisition()` semantics (called from Disconnect, closeEvent, `MainWindow.closeEvent`).
- Keep `lbl_connection` (small text next to the Connect switch, using `_set_label_good/_bad`) so the connection result
  text still has somewhere to appear.

## 6. Derived Scope values (match PicoScope's Scope tile)

PicoScope's Scope tile shows timebase plus `Samples` and `Sample rate`. Do the same:

- `Samples = max(1, int(window_ms * 1e6 / sample_interval_ns))` — identical to `RatemeterConfig.num_samples`.
- `Sample rate = 1e9 / sample_interval_ns` Hz, formatted `S/kS/MS/s`.
- Do not invent ms/div (this plot has no divisions); show the total window instead.
- Add one helper (e.g. `_derived_scope_values()`) and a unit test asserting it equals `_build_config().num_samples`.

## 7. New reusable widgets (put in `ui/`, export from `ui/__init__.py`)

- `ui/toggle_switch.py` — `ToggleSwitch` (themed, label optional, disabled look).
- `ui/param_tile.py` — `ParamTile`: title, primary value, optional secondary lines, optional −/+ buttons, `clicked` signal
  for the body (opens/closes dock) and `step_down`/`step_up` signals for −/+. Body click must not fire when −/+ clicked.
  Has `set_values(primary, secondary=None, warn=False)` and a checked/"open" look while its panel is open.
- `ui/dock_panel.py` — `DockHost`: header title + `QScrollArea` + `QStackedWidget` of panels keyed by tile id;
  `toggle(key)`, `open(key)`, `close()`, `current_key`, signal `changed(key|None)`. Hidden when closed.

All three themed via `ThemedMixin`. Page-specific wiring (which tile maps to which panel, summary formatting) stays in
`ratemeter_page.py`.

## 8. Implementation phases (each ends in a working app)

**Phase 0 — Safety net (no UI change).**
- Add `tests/test_ratemeter_page_ui.py` using `QT_QPA_PLATFORM=offscreen`, patching `PicoScopeService` (no hardware).
- Characterization tests: seed QSettings with several value sets (probe x0.1/x1/x10, each interval, MF on/off, scales
  `"0.75, 1.0, 2.0"`, 3+ bands with transit widths, trigger on/off), construct the page, assert `_build_config()`,
  `_build_mf_config()`, `_trigger_direction_value()` equal expected values. Save, re-create the page, assert round-trip.
  These must pass **before and after** the refactor.
- Record the list of QSettings keys written by `_save_settings()` and assert the set is unchanged (minus section keys).

**Phase 1 — Widgets.** Build `ToggleSwitch`, `ParamTile`, `DockHost` with small smoke tests (construct, apply every theme,
click signals). No page changes.

**Phase 2 — Re-parent.** In `ratemeter_page.py`: change `_make_acquisition_group` / `_trigger` / `_detection_mode` /
`_averaging` / `_transit` to return plain panels; split Acquisition into Channel panel (channel, probe, range, coupling,
BW limit) and Scope panel (window, interval, captures). Build the new frame (top bar, rail, dock, bottom strip) in
`_build_ui()` replacing the horizontal splitter. Move `table_bands` + Add/Remove to the bottom strip and `rates_frame`
to bottom-right. Extract `_make_recorder_group()`. Wire dock open/close. Keep build order: all panels → tiles → plots →
`_load_settings()` (the existing comments explain that plot and band table do not exist while the panels are built, so
`_schedule_restart` must stay out of builders).

**Phase 3 — Tiles and switches.** Add `_refresh_tiles()` and call it from `_schedule_restart()`, the `spin_trend_window`
handler, and the end of `_load_settings()`; guard against tiles not existing yet. Add −/+ steppers (via setting the
existing control so the normal signal path fires). Add `ToggleSwitch` wiring + `_sync_switches()` (§5). Add derived
Scope values (§6) and the ⚠ badge.

**Phase 4 — Persistence, theme, cleanup.** Optionally persist which dock is open under a *new* key
`ratemeter/dock_open` (default closed). Verify all themes. Remove the `CollapsibleBox` import/`_collapsible_sections`
from this page (leave `ui/collapsible_box.py` in place). Update `docs/rate_record.md` Part B (collapsible left panel is
superseded) or add `docs/ratemeter_ui.md`. Update the module docstring layout section in `ratemeter_page.py`.

## 9. Pitfalls to watch for

- **Probe change repopulates `combo_range` with signals blocked** (`_populate_range_combo`); it then calls
  `_schedule_restart`, so tile refresh must be reachable from there. The −/+ range stepper works on the combo *index*
  (items are native ranges; labels are probe-scaled via `format_voltage_label`).
- **Switch/state drift**: any path that fails or early-returns in `_on_start_clicked` / `_on_connect_clicked` must end with
  `_sync_switches()`. Starting emits `daq_busy(True)` which re-enters `_on_daq_busy` on this same page — worker is already
  assigned at that point, preserve that ordering.
- **`stop_acquisition()` blocks up to 5 s** waiting on the worker (existing behavior); don't add UI-thread work around it.
- **Timed recording** pauses waveform plotting (`_timed_recording_running`) — unchanged; recorder widgets keep their names
  so `_on_timed_record_clicked` / `_reset_timed_recording_ui` need no edits.
- Matched-filter controls are shown/hidden by `_on_detection_mode_changed` via `_mf_controls.setVisible`; inside a scrollable
  dock this must still reflow correctly.
- pyqtgraph colors are set at build time from `style.PLOT_BG`/`style.TXT` and are not live-updated on theme change today;
  do not regress, and do not take this on as part of the refactor.
- The page is wrapped in a `QScrollArea` by `MainWindow._scrollable`; keep a sensible minimum size so the top bar, rail,
  open dock, and plots fit on a 1200×800 window.

## 10. Acceptance checklist

- [ ] Phase 0 tests pass unchanged after the refactor; QSettings key set identical (minus unused section keys).
- [ ] Every setting from the old left panel is reachable (tile → dock) and persists across restarts.
- [ ] Tiles always show the current value, including after load-from-settings, probe change, and −/+ stepping.
- [ ] Clicking an open tile closes the dock; clicking another tile switches panels; only one open at a time.
- [ ] Scope tile shows window, Samples, Sample rate; values match `RatemeterConfig.num_samples`.
- [ ] Connect/Run switches always reflect true state, including failure and "PicoScope busy" cases; DAQ-page mutual
      exclusion still works.
- [ ] Band table editable while running; edits restart the worker as before; + Add creates additional bands; readouts update live.
- [ ] Data recorder sits bottom-left with unchanged arrangement and behavior (Record, Timed Recording + metadata dialog).
- [ ] No acquisition/detection diffs: `git diff --stat` shows changes only in `pages/ratemeter_page.py`, `ui/*`, `tests/*`, `docs/*`.
- [ ] All themes look correct (tile, dock, switch, table, readouts).

## 11. Assumptions made (flag if wrong)

1. Averaging + Transit settings are combined into one **Rates** tile (not in the original tile list).
2. **Captures per batch** gets its own small top-bar tile (PicoScope's "Waveform N of M" slot); alternatively fold it into the Scope panel.
3. Channel choices remain **A/B** only, as in the current combo (PicoScope 7's A–D tiles are not replicated).
4. Dock starts closed on launch; one panel at a time.
5. −/+ steppers are a nice-to-have in Phase 3; if they complicate things, ship tiles + docks first.