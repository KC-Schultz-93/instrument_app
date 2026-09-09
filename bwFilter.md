Add bandwidth limiter + rapid block (segmented) capture to the ratemeter page
Scope

Two additions to the existing PicoScope 4262 (ps4000 driver) path — both are additive to services/picoscope_service.py, no driver/package change needed. Do not touch the psospa/3417E work (separate doc) or the 4262 connection fix already applied.

Bandwidth limiter toggle (small — one new SDK call, one new UI control)
Rapid block / segmented-memory capture (bigger — changes the acquisition loop shape, not just a new call)

Both call sequences below were pulled from the actual picosdk-python-wrappers source and a real Pico rapid-block example, not reconstructed from memory — verify against picosdk/ps4000.py directly before relying on argument order, same discipline as the earlier ps4000 rename fix.

Part 1: Bandwidth limiter

ps4000SetBwFilter is documented as "PicoScope 4262 only" — it's specific to this hardware, not a generic ps4000-family shim. Confirmed signature:

ps4000SetBwFilter(handle: c_int16, channel: c_int32, enable: c_int16) -> PICO_STATUS

Simple per-channel on/off (200 kHz limiter vs. the 4262's full ~5 MHz bandwidth) — no enum of filter levels, just enable/disable.

Changes:

services/daq_models.AcquisitionConfig — add a bandwidth_limit_enabled: bool = False field.
services/picoscope_service.py — in configure_channel(), right after the existing ps4000SetChannel loop, add a call to ps4000SetBwFilter for the active channel using config.bandwidth_limit_enabled. Disabled channels can be left at whatever default (matches the existing pattern of only fully configuring the active channel).
pages/ratemeter_page.py — add a checkbox ("200 kHz bandwidth limit") next to the existing range/coupling controls, wired the same way those already are into AcquisitionConfig.

No change to daq_page.py/acquisition_worker.py structure needed — this is a channel-configuration option, same shape as voltage range or coupling.

Part 2: Rapid block (segmented memory) capture

This is not "add one function call" — it changes what a single acquisition round produces (a batch of N triggered waveforms instead of one), so it touches the acquisition loop, not just picoscope_service.py.

Verified call sequence (ps4000, adapted from the real ps4000a rapid

block example — same concepts, ps4000 non-A signatures below are the actually-confirmed ones for this driver, do not copy the ps4000a example's argument counts verbatim, they differ):

ps4000MemorySegments(handle, nSegments: c_uint16, nMaxSamples: *c_int32) -> PICO_STATUS
    # Splits scope memory into nSegments segments. Returns the max samples
    # that fit per segment — MORE segments means FEWER samples per segment.
    # config.num_samples must be checked against the returned nMaxSamples;
    # if it doesn't fit, that's a real constraint to surface to the user
    # (reduce capture length or reduce nCaptures), not silently truncate.

ps4000SetNoOfCaptures(handle, nCaptures: c_uint16) -> PICO_STATUS
    # nCaptures <= nSegments from the call above.

# Channel setup (ps4000SetChannel) and trigger (ps4000SetSimpleTrigger) are
# configured once, same as today — they apply to every capture in the batch.

ps4000SetDataBufferBulk(handle, channel: c_int32, buffer: *c_int16, bufferLth: c_int32, waveform: c_uint16) -> PICO_STATUS
    # Call ONCE PER SEGMENT (waveform = segment index 0..nCaptures-1), each
    # with its own buffer. This is the rapid-block counterpart to the normal
    # path's single ps4000SetDataBuffer call.

ps4000RunBlock(handle, noOfPreTriggerSamples, noOfPostTriggerSamples, timebase, oversample, timeIndisposedMs: *c_int32, segmentIndex: c_uint16, lpReady, pParameter) -> PICO_STATUS
    # Called ONCE (segmentIndex=0), same signature as today's single-capture
    # run_block(). The device internally fills all nCaptures segments from
    # consecutive trigger events with much less dead time between them than
    # calling RunBlock in a loop.

ps4000IsReady(handle, ready: *c_int16) -> PICO_STATUS
    # Same polling pattern as today.

ps4000GetValuesBulk(handle, noOfSamples: *c_uint32, fromSegmentIndex: c_uint16, toSegmentIndex: c_uint16, overflow: *c_int16) -> PICO_STATUS
    # 5 args for ps4000 (non-A) — note this differs from ps4000a's 6-arg
    # version (which also takes downSampleRatio/downSampleRatioMode); don't
    # add those here. overflow should be an array of c_int16 sized to
    # nCaptures (one flag per segment), not a scalar.
    # Fills the buffers set via SetDataBufferBulk, one segment at a time.

ps4000Stop(handle) -> PICO_STATUS
    # Not currently called anywhere in picoscope_service.py even for normal
    # block mode. Worth adding after a rapid-block batch (and arguably after
    # normal run_block too) as good practice before re-arming — check the
    # ps4000 programmer's guide on whether omitting it has caused any issue
    # in practice before assuming it's required.
Design decision needed: what does this return, and who consumes it?

Today PicoScopeService.run_block() returns one WaveformRecord, and whatever drives the ratemeter's acquisition loop calls it once per iteration. A rapid-block round returns N waveforms from one round-trip. Two things need deciding when implementing, not guessing:

Add run_rapid_block(config, n_captures) -> List[WaveformRecord] as a new method alongside (not replacing) run_block(), so normal single-shot mode in daq_page.py is untouched.
Check how pages/ratemeter_page.py currently drives acquisition before changing it. This doc doesn't have full visibility into that file's current loop — whether it goes through a worker thread like acquisition_worker.py, or drives PicoScopeService directly on a timer. Whichever it is, the per-batch result (N waveforms) needs to feed the existing rate-counting/plotting logic per-trace, the same as it does today for single captures — don't change the rate-computation logic itself, just how many traces arrive per acquisition round.
Expose the number of captures per batch as a new UI control (e.g. a spinbox, default 1 to preserve today's behavior exactly when unused).
Verification
Confirm config.num_samples fits within the nMaxSamples that ps4000MemorySegments reports back for the chosen number of captures — add an explicit error rather than silent truncation if it doesn't.
Compare effective dead-time-between-traces with rapid block enabled vs. disabled at the same capture length, to confirm it's actually delivering the expected benefit before making it the default.