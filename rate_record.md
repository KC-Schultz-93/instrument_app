Ratemeter: timed recording (raw peaks) + collapsible left panel

Two independent additions to pages/ratemeter_page.py. Part A touches services + a new dialog; Part B is UI-only. Safe to build/ship separately.

Part A: Timed recording with raw peak export
What changed from the original ask (confirmed with user)

Not per-band counts. Record every individual detected peak's raw amplitude, so bin edges can be chosen after the fact during analysis instead of being locked to whatever bands were configured live. Bands still matter for one thing: the peak-detection height floor (0.9 × lowest band's low_mv) is unchanged — bands just no longer sort what gets recorded.

Data model: two CSVs, not one

Recordings log (Recorded Data/Ratemeter/recordings.csv, one row appended per recording):

recording_id, timestamp, description, vpp, frequency_hz, duration_s,
total_peak_count, channel, voltage_range_v, coupling, sample_interval_ns,
window_duration_ms, trigger_enabled, trigger_threshold_mv

Peaks log (Recorded Data/Ratemeter/peaks.csv, one row per detected peak, appended continuously):

recording_id, peak_amplitude_mv, peak_time_ns

Join on recording_id at analysis time (pandas merge) to filter by Vpp/frequency and histogram amplitudes with arbitrary bin edges.

recording_id can just be the recording's start timestamp — unique enough, no separate ID scheme needed.

Why this needs new code, not a reuse of the live rate path

RatemeterWorker.run() currently computes each peak's amplitude, checks it against band ranges, and keeps only a bare timestamp in a deque — the amplitude value itself is discarded immediately. A recording pass needs to retain the full PeakRecord (amplitude_v, time_ns) for every peak found across the whole recording, not just a count.

Recommended shape
Recording duration: a spinbox, not hardcoded 30s (user explicitly asked for this after the earlier discussion — default 30, adjustable).
Vpp / frequency / description: prompted in a dialog after the timer elapses, before writing to CSV — matches the user's original description of the workflow (record → prompt → export). Three fields: description (free text), Vpp (float), frequency (float).
Per-trace window: reuse whatever window_duration_ms is currently set on the page. No special override — the user already tuned this window against their actual pulse width (~80μs pulses, ~0.25ms window) earlier in this project. Don't reintroduce a separate "recording window" setting.
Skip live plotting during recording — don't connect/update the waveform plot while a timed recording is running, per the user's own suggestion, to avoid competing with the acquisition loop for the GIL during a max-effort capture window. The live rate display can also be skipped or left running — either is fine, it's not the bottleneck; the waveform redraw is.
Simple throughput version — one capture at a time via run_block(), same as today, no rapid-block dependency (user confirmed this scope earlier). Rapid block can be layered in later without changing the CSV schema.
One thing to check, not guess

Does "Record" require the live worker to already be running (user clicked Start first), or is it a self-contained action that connects/configures/runs the scope on its own for the recording duration regardless of whether Start was pressed? Check how starting/stopping currently interacts with _daq_busy / the shared DAQChannels bus before deciding — don't assume, this affects whether Record needs its own connect/disconnect handling or can just piggyback on an already-running worker.

Rough effort

Half a day to a day: new accumulator path in a worker (or a small recording-mode flag on the existing one), a completion dialog, CSV append logic, and the Recorded Data/Ratemeter/ folder convention.

Part B: Collapsible left panel sections
Problem

Left panel (pages/ratemeter_page.py, fixed 320px scrollable) stacks Connection, Acquisition, Trigger, Averaging, Bands, Run as plain QGroupBox widgets — getting long as features get added (bandwidth limit checkbox, rapid-block controls, record controls all land here too).

Recommended widget: new reusable CollapsibleBox

Add to ui/ (matches the repo's existing "reusable themed widgets" folder per CLAUDE.md), e.g. ui/collapsible_box.py:

A QToolButton header (checkable, shows the section title + an arrow/chevron that flips based on expanded state).
A content QWidget holding whatever currently goes directly into the QGroupBox.
Toggling the header just calls content.setVisible(checked) — start without animation. A QPropertyAnimation on maximumHeight is a nice later polish pass, not needed for the first version, and adds real complexity (animation groups, sizeHint timing) for no functional benefit.
Which sections collapse

Recommend: Acquisition, Trigger, Averaging, Bands become collapsible. Connection and Run stay as plain always-visible groups — they're small and used every session (connect/disconnect, start/stop), collapsing them just adds a click for no space savings.

Wiring

Each _make_acquisition_group() / _make_trigger_group() / _make_averaging_group() / _make_bands_group() method changes its container from QGroupBox to CollapsibleBox, but the widgets built inside (self.combo_channel, self.spin_trigger_threshold, etc.) keep the same names and get added to the box's content widget instead of the group box's layout directly. No signal-wiring elsewhere needs to change — this is a container swap, not a rename.

Persist expand/collapse state

Add to the existing _save_settings() / _load_settings() pair (already uses QSettings under the ratemeter/ prefix for everything else): one bool per collapsible section, e.g. ratemeter/section_expanded_acquisition. Default to expanded on first run so nothing looks broken/empty the first time someone opens the page.

Rough effort

A few hours: one new small widget class, four call sites swapped over, four new settings keys.