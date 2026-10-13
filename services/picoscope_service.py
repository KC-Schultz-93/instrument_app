"""
PicoScopeService.py
-------------------
All communication with the PicoScope 4262 via the ps4000 driver family.
This is the only module that imports or calls picosdk.

No Qt. No file I/O. All methods are synchronous and intended to be called
from a worker thread (run_block is a blocking call).

Hardware notes:
  - ADC max count for ps4000 series: 32512 (not 32768)
  - Timebase 0 â†' 10 ns, 1 â†' 20 ns, n â‰¥ 2 â†' (n-2)*8 + 32 ns
  - ps4000 uses int16 buffers; SetDataBuffer must be called before GetValues
  - The ctypes buffer must remain alive until GetValues completes
"""

from __future__ import annotations

import ctypes
import time
from datetime import datetime
from typing import List, Tuple

import numpy as np

from instrument_app.services.daq_models import AcquisitionConfig, WaveformRecord


# ---------------------------------------------------------------------------
# ps4000 constants not always exported by the wrapper
# ---------------------------------------------------------------------------

# Voltage range enum values (PS4000_RANGE)
_RANGE_MAP = {
    0.01: 0,   # PS4000_10MV
    0.02: 1,   # PS4000_20MV
    0.05: 2,   # PS4000_50MV
    0.1:  3,   # PS4000_100MV
    0.2:  4,   # PS4000_200MV
    0.5:  5,   # PS4000_500MV
    1.0:  6,   # PS4000_1V
    2.0:  7,   # PS4000_2V
    5.0:  8,   # PS4000_5V
    10.0: 9,   # PS4000_10V
    20.0: 10,  # PS4000_20V
    50.0: 11,  # PS4000_50V
}

# Channel enum values (PS4000_CHANNEL)
_CHANNEL_MAP = {
    "A": 0,
    "B": 1,
    "C": 2,
    "D": 3,
}

# Coupling enum values (PS4000_COUPLING)
_COUPLING_MAP = {
    "AC": 0,
    "DC": 1,
}

# Trigger direction (THRESHOLD_DIRECTION) — values confirmed from
# ps4000Api.h's enThresholdDirection. RUNT_POSITIVE/RUNT_NEGATIVE are not part
# of the driver's own enum names (those are POSITIVE_RUNT/NEGATIVE_RUNT); the
# RUNT_ prefix here is this app's own AcquisitionConfig.trigger_direction
# vocabulary, kept distinct from the driver's naming.
_DIRECTION_MAP = {
    "ABOVE":          0,
    "BELOW":          1,
    "RISING":         2,
    "FALLING":        3,
    "RISING_OR_FALLING": 4,
    "RUNT_POSITIVE":  9,   # POSITIVE_RUNT
    "RUNT_NEGATIVE":  10,  # NEGATIVE_RUNT
}

_RUNT_DIRECTIONS = {"RUNT_POSITIVE", "RUNT_NEGATIVE"}

# THRESHOLD_MODE (ps4000Api.h enThresholdMode). Runt trigger always uses
# LEVEL — WINDOW is a different (inside/outside-band) trigger type.
_THRESHOLD_MODE_LEVEL = 0

# TRIGGER_STATE (ps4000Api.h enTriggerState), used in TRIGGER_CONDITIONS.
_TRIGGER_STATE_DONT_CARE = 0
_TRIGGER_STATE_TRUE = 1

# ADC full-scale count for ps4000 series
_ADC_MAX = 32512

# PS4000_TIME_UNITS (ps4000Api.h enPS4000TimeUnits) -> nanoseconds-per-unit,
# for converting ps4000GetValuesTriggerTimeOffsetBulk64's raw int64 values.
_TIME_UNIT_TO_NS = {
    0: 1e-6,   # PS4000_FS (femtoseconds)
    1: 1e-3,   # PS4000_PS (picoseconds)
    2: 1.0,    # PS4000_NS
    3: 1e3,    # PS4000_US
    4: 1e6,    # PS4000_MS
    5: 1e9,    # PS4000_S
}


class _TriggerChannelProperties(ctypes.Structure):
    """
    Mirrors ps4000Api.h's tTriggerChannelProperties exactly (#pragma pack(1)).
    The installed picosdk ps4000.py wrapper types this parameter as a plain
    c_void_p (no struct class is provided), so this app defines its own.
    """
    _pack_ = 1
    _fields_ = [
        ("thresholdUpper", ctypes.c_int16),
        ("thresholdUpperHysteresis", ctypes.c_uint16),
        ("thresholdLower", ctypes.c_int16),
        ("thresholdLowerHysteresis", ctypes.c_uint16),
        ("channel", ctypes.c_int32),        # PS4000_CHANNEL
        ("thresholdMode", ctypes.c_int32),  # THRESHOLD_MODE
    ]


class _TriggerConditions(ctypes.Structure):
    """Mirrors ps4000Api.h's tTriggerConditions exactly (#pragma pack(1))."""
    _pack_ = 1
    _fields_ = [
        ("channelA", ctypes.c_int32),
        ("channelB", ctypes.c_int32),
        ("channelC", ctypes.c_int32),
        ("channelD", ctypes.c_int32),
        ("external", ctypes.c_int32),
        ("aux", ctypes.c_int32),
        ("pulseWidthQualifier", ctypes.c_int32),
    ]


assert ctypes.sizeof(_TriggerChannelProperties) == 16, (
    "TRIGGER_CHANNEL_PROPERTIES layout mismatch vs ps4000Api.h"
)
assert ctypes.sizeof(_TriggerConditions) == 28, (
    "TRIGGER_CONDITIONS layout mismatch vs ps4000Api.h"
)


class PicoScopeService:
    """
    Owns the PicoScope 4262 device handle and all SDK interactions.

    Usage:
        svc = PicoScopeService()
        svc.connect()
        config = AcquisitionConfig(...)
        svc.configure_channel(config)
        record = svc.run_block(config)   # call from worker thread
        svc.disconnect()
    """

    def __init__(self) -> None:
        self._handle: ctypes.c_int16 = ctypes.c_int16(0)
        self._connected: bool = False
        self._ps = None  # picosdk ps4000 module, loaded lazily

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Open the first available PicoScope 4262.
        Raises RuntimeError if no device is found or the SDK call fails.
        """
        ps = self._get_ps()
        status = ps.ps4000OpenUnit(ctypes.byref(self._handle))
        self._check_status(status, "ps4000OpenUnit")
        self._connected = True

    def disconnect(self) -> None:
        """Close the device handle. Safe to call when not connected."""
        if not self._connected:
            return
        ps = self._get_ps()
        ps.ps4000CloseUnit(self._handle)
        self._handle = ctypes.c_int16(0)
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure_channel(self, config: AcquisitionConfig) -> None:
        """
        Enable one channel with the given coupling and voltage range.
        All other channels are disabled to keep the buffer simple.
        """
        ps = self._get_ps()

        channel_enum = _CHANNEL_MAP.get(config.channel.upper())
        if channel_enum is None:
            raise ValueError(f"Unknown channel: {config.channel!r}. Use 'A' or 'B'.")

        range_enum = self._nearest_range_enum(config.voltage_range_v)
        coupling_enum = _COUPLING_MAP.get(config.coupling.upper(), 1)

        # ps4000 4262 has two channels (A=0, B=1). Iterating beyond that
        # returns PICO_INVALID_CHANNEL on this device.
        for ch in range(2):
            status = ps.ps4000SetChannel(
                self._handle,
                ch,
                1 if ch == channel_enum else 0,
                coupling_enum if ch == channel_enum else 1,
                range_enum if ch == channel_enum else 7,
            )
            self._check_status(status, f"ps4000SetChannel(ch={ch})")

        # 200 kHz bandwidth limiter — PS4000_4262 only. Simple per-channel
        # enable/disable, no filter-level enum.
        status = ps.ps4000SetBwFilter(
            self._handle, channel_enum, 1 if config.bandwidth_limit_enabled else 0
        )
        self._check_status(status, "ps4000SetBwFilter")

    def get_timebase(self, config: AcquisitionConfig) -> Tuple[int, int]:
        """
        Find the ps4000 timebase index that best matches config.sample_interval_ns.

        Returns:
            (timebase_index, actual_interval_ns)

        ps4000 timebase mapping:
            0 â†' 10 ns
            1 â†' 20 ns
            n â‰¥ 2 â†' (n-2)*8 + 32 ns
        """
        target_ns = config.sample_interval_ns

        # Build candidates around the target, from timebase index 0 upward.
        # Stop once the interval clearly exceeds the target by a large margin.
        best_index = 0
        best_actual = 10  # interval at timebase 0

        for idx in range(0, 2**16):
            actual = self._timebase_to_ns(idx)
            if abs(actual - target_ns) < abs(best_actual - target_ns):
                best_index = idx
                best_actual = actual
            if actual > target_ns * 4:
                break

        return best_index, best_actual

    def set_trigger(self, config: AcquisitionConfig, auto_trigger_ms: int = 1000) -> None:
        """
        Configure a trigger, or disable triggering.

        Dispatches to set_runt_trigger() when config.trigger_direction is
        "RUNT_POSITIVE"/"RUNT_NEGATIVE" (runt needs two thresholds, set via
        TriggerChannelProperties/Conditions/Directions — ps4000SetSimpleTrigger
        only supports one threshold and cannot express it). Callers do not
        need to choose between the two paths themselves.
        """
        ps = self._get_ps()

        if not config.trigger_enabled:
            # Disable trigger by setting enabled=0. Pass auto_trigger_ms even
            # here: 4262 firmware may still apply the auto-trigger timer when
            # enabled=0, so hardcoding 0 ("wait forever") hangs indefinitely.
            self._clear_trigger_conditions()
            status = ps.ps4000SetSimpleTrigger(
                self._handle,
                0,              # enabled = false
                0,              # source (ignored)
                0,              # threshold (ignored)
                2,              # direction RISING (ignored)
                0,              # delay
                auto_trigger_ms,
            )
            self._check_status(status, "ps4000SetSimpleTrigger(disabled)")
            return

        if config.trigger_direction.upper() in _RUNT_DIRECTIONS:
            self.set_runt_trigger(config, auto_trigger_ms)
            return

        # A previous run may have left a runt TriggerConditions/Directions
        # configuration active; SetSimpleTrigger does not itself clear that
        # state, so clear it explicitly before falling back to the simple path.
        self._clear_trigger_conditions()

        threshold_v = config.trigger_threshold_v or 0.0
        # threshold_v is the desired true (probe-scaled) trigger level; convert
        # back to native scope-input volts before the ADC-count math.
        threshold_adc = int((threshold_v / config.probe_factor) / config.voltage_range_v * _ADC_MAX)
        threshold_adc = max(-_ADC_MAX, min(_ADC_MAX, threshold_adc))

        direction_enum = _DIRECTION_MAP.get(config.trigger_direction.upper(), 2)
        channel_enum = _CHANNEL_MAP.get(config.channel.upper(), 0)

        status = ps.ps4000SetSimpleTrigger(
            self._handle,
            1,                   # enabled
            channel_enum,        # source channel
            threshold_adc,       # threshold in ADC counts
            direction_enum,      # direction
            0,                   # delay (samples)
            auto_trigger_ms,     # autoTrigger_ms
        )
        self._check_status(status, "ps4000SetSimpleTrigger")

    def set_runt_trigger(self, config: AcquisitionConfig, auto_trigger_ms: int = 1000) -> None:
        """
        Configure a runt trigger: the signal must cross the near threshold
        (trigger_threshold_v) without fully crossing the far threshold
        (trigger_threshold_lower_v) before returning — a pulse too short/small
        to count as a full edge trigger, but distinguishable from noise.

        Sign convention: trigger_threshold_v is always the "outer" (peak-side)
        threshold and trigger_threshold_lower_v is always the "inner"
        (baseline-side) threshold, regardless of RUNT_POSITIVE/RUNT_NEGATIVE —
        this matches the header's own thresholdUpper/thresholdLower naming,
        which encodes ADC magnitude, not polarity. For RUNT_POSITIVE,
        trigger_threshold_v must be the larger (upper) value; for
        RUNT_NEGATIVE it must be the more negative (lower) value — the two
        fields are mapped onto thresholdUpper/thresholdLower by numeric sign,
        not by which field the caller used for which purpose.

        Requires three driver calls in this order: SetTriggerChannelProperties
        (thresholds + hysteresis), SetTriggerChannelConditions (which channel
        must satisfy the trigger), SetTriggerChannelDirections (the runt
        direction). All three must be set together or the driver ignores the
        direction call.
        """
        ps = self._get_ps()

        if config.trigger_threshold_lower_v is None:
            raise ValueError(
                "Runt trigger requires AcquisitionConfig.trigger_threshold_lower_v."
            )

        channel_enum = _CHANNEL_MAP.get(config.channel.upper())
        if channel_enum is None:
            raise ValueError(f"Unknown channel: {config.channel!r}. Use 'A' or 'B'.")

        def to_adc(threshold_v: float) -> int:
            adc = int((threshold_v / config.probe_factor) / config.voltage_range_v * _ADC_MAX)
            return max(-_ADC_MAX, min(_ADC_MAX, adc))

        threshold_v = config.trigger_threshold_v or 0.0
        threshold_lower_v = config.trigger_threshold_lower_v
        adc_a, adc_b = to_adc(threshold_v), to_adc(threshold_lower_v)
        upper_adc, lower_adc = max(adc_a, adc_b), min(adc_a, adc_b)
        if upper_adc == lower_adc:
            raise ValueError(
                "Runt trigger thresholds must differ: "
                f"trigger_threshold_v={threshold_v!r}, "
                f"trigger_threshold_lower_v={threshold_lower_v!r} "
                "resolve to the same ADC count."
            )

        hysteresis_v = config.trigger_hysteresis_v or 0.0
        hysteresis_adc = max(0, int((hysteresis_v / config.probe_factor) / config.voltage_range_v * _ADC_MAX))

        props = _TriggerChannelProperties(
            thresholdUpper=upper_adc,
            thresholdUpperHysteresis=hysteresis_adc,
            thresholdLower=lower_adc,
            thresholdLowerHysteresis=hysteresis_adc,
            channel=channel_enum,
            thresholdMode=_THRESHOLD_MODE_LEVEL,
        )
        status = ps.ps4000SetTriggerChannelProperties(
            self._handle,
            ctypes.byref(props),
            1,                   # nChannelProperties
            0,                   # auxOutputEnable
            auto_trigger_ms,
        )
        self._check_status(status, "ps4000SetTriggerChannelProperties")

        conditions = _TriggerConditions(
            channelA=_TRIGGER_STATE_TRUE if channel_enum == 0 else _TRIGGER_STATE_DONT_CARE,
            channelB=_TRIGGER_STATE_TRUE if channel_enum == 1 else _TRIGGER_STATE_DONT_CARE,
            channelC=_TRIGGER_STATE_TRUE if channel_enum == 2 else _TRIGGER_STATE_DONT_CARE,
            channelD=_TRIGGER_STATE_TRUE if channel_enum == 3 else _TRIGGER_STATE_DONT_CARE,
            external=_TRIGGER_STATE_DONT_CARE,
            aux=_TRIGGER_STATE_DONT_CARE,
            pulseWidthQualifier=_TRIGGER_STATE_DONT_CARE,
        )
        status = ps.ps4000SetTriggerChannelConditions(
            self._handle, ctypes.byref(conditions), 1
        )
        self._check_status(status, "ps4000SetTriggerChannelConditions")

        runt_direction = _DIRECTION_MAP[config.trigger_direction.upper()]
        # Non-active channel slots get RISING (2) — the header's own NONE
        # default — since those channels are CONDITION_DONT_CARE above and
        # never gate the trigger; a valid enum is passed regardless, not 0.
        directions = [2, 2, 2, 2, 2, 2]  # [A, B, C, D, ext, aux]
        directions[channel_enum] = runt_direction
        status = ps.ps4000SetTriggerChannelDirections(self._handle, *directions)
        self._check_status(status, "ps4000SetTriggerChannelDirections")

    def _clear_trigger_conditions(self) -> None:
        """
        Reset TriggerChannelConditions to all-DONT_CARE so a previous runt
        trigger configuration doesn't linger alongside a newly-set simple
        edge trigger or a disabled trigger.
        """
        ps = self._get_ps()
        conditions = _TriggerConditions(
            channelA=_TRIGGER_STATE_DONT_CARE,
            channelB=_TRIGGER_STATE_DONT_CARE,
            channelC=_TRIGGER_STATE_DONT_CARE,
            channelD=_TRIGGER_STATE_DONT_CARE,
            external=_TRIGGER_STATE_DONT_CARE,
            aux=_TRIGGER_STATE_DONT_CARE,
            pulseWidthQualifier=_TRIGGER_STATE_DONT_CARE,
        )
        status = ps.ps4000SetTriggerChannelConditions(
            self._handle, ctypes.byref(conditions), 1
        )
        self._check_status(status, "ps4000SetTriggerChannelConditions(clear)")

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def run_block(self, config: AcquisitionConfig) -> WaveformRecord:
        """
        Run one block acquisition. BLOCKING â€" call from a worker thread.

        Steps:
          1. Determine timebase
          2. ps4000RunBlock
          3. Poll ps4000IsReady (yields GIL via time.sleep)
          4. ps4000SetDataBuffer + ps4000GetValues
          5. ADC â†' volts conversion
          6. Return WaveformRecord

        The ctypes buffer is kept alive in a local variable until GetValues
        completes to prevent premature garbage collection.
        """
        ps = self._get_ps()
        timestamp = datetime.now()

        post_samples = config.num_samples - config.pre_trigger_samples
        timebase_index, actual_interval_ns = self.get_timebase(config)

        # Cancel any leftover acquisition from a previous cycle or stale state.
        # ps4000Stop is safe to call when idle; without it IsReady can stay
        # stuck if the device was left mid-acquisition.
        ps.ps4000Stop(self._handle)

        # RunBlock
        time_indisposed = ctypes.c_int32(0)
        status = ps.ps4000RunBlock(
            self._handle,
            config.pre_trigger_samples,
            post_samples,
            timebase_index,
            1,                              # oversample = 1
            ctypes.byref(time_indisposed),
            0,                              # segmentIndex
            None,                           # lpReady callback (use polling)
            None,                           # pParameter
        )
        self._check_status(status, "ps4000RunBlock")

        # Poll until ready
        ready = ctypes.c_int16(0)
        for _ in range(100_000):
            status = ps.ps4000IsReady(self._handle, ctypes.byref(ready))
            self._check_status(status, "ps4000IsReady")
            if ready.value:
                break
            time.sleep(0.001)
        else:
            raise RuntimeError("PicoScope: run_block timed out waiting for ready.")

        # Allocate int16 buffers. Use ps4000SetDataBuffers (plural) — the form
        # used in all official picosdk examples — to avoid a driver quirk where
        # the singular ps4000SetDataBuffer leaves the min-buffer unregistered
        # and GetValues writes nothing.
        channel_enum = _CHANNEL_MAP[config.channel.upper()]
        buffer     = (ctypes.c_int16 * config.num_samples)()
        buffer_min = (ctypes.c_int16 * config.num_samples)()  # unused placeholder

        status = ps.ps4000SetDataBuffers(
            self._handle,
            channel_enum,
            ctypes.byref(buffer),
            ctypes.byref(buffer_min),
            config.num_samples,
        )
        self._check_status(status, "ps4000SetDataBuffers")

        overflow = ctypes.c_int16(0)
        n_values = ctypes.c_int32(config.num_samples)
        status = ps.ps4000GetValues(
            self._handle,
            0,                              # startIndex
            ctypes.byref(n_values),
            1,                              # downSampleRatio
            0,                              # downSampleRatioMode = NONE
            0,                              # segmentIndex
            ctypes.byref(overflow),
        )
        self._check_status(status, "ps4000GetValues")

        n = n_values.value
        raw = np.frombuffer(buffer, dtype=np.int16, count=n).astype(np.float64)

        # ADC â†' volts (true, probe-scaled volts — see services/probe_config.py)
        voltage = raw / _ADC_MAX * config.voltage_range_v * config.probe_factor
        if config.invert_polarity:
            voltage = -voltage

        time_ns = np.arange(n, dtype=np.float64) * actual_interval_ns

        metadata: dict = {
            "actual_interval_ns": actual_interval_ns,
            "timebase_index": timebase_index,
            "overflow": bool(overflow.value),
            "n_values": n,
        }

        return WaveformRecord(
            trace_id=0,             # caller sets this
            run_id="",              # caller sets this
            timestamp=timestamp,
            voltage=voltage,
            time_ns=time_ns,
            sample_interval_ns=actual_interval_ns,
            config=config,
            metadata=metadata,
        )

    def run_rapid_block(
        self,
        config: AcquisitionConfig,
        n_captures: int,
        ready_timeout_s: float = 30.0,
    ) -> List[WaveformRecord]:
        """
        Run one rapid-block (segmented memory) acquisition round, capturing
        n_captures triggered waveforms in a single RunBlock round-trip.
        BLOCKING — call from a worker thread.

        Raises ValueError if config.num_samples does not fit within the
        max samples per segment reported by ps4000MemorySegments for the
        requested n_captures (caller should reduce capture length or
        n_captures rather than expect silent truncation).

        If the device never reports ready within ready_timeout_s (e.g. a rare
        particle rate means not all n_captures segments filled), the batch is
        stopped and whatever segments did complete are fetched and returned —
        fewer than n_captures records, each flagged
        metadata["partial_batch"] = True. Raises RuntimeError only if zero
        segments completed.

        Each returned record's metadata["trigger_offset_ns"] holds the
        segment's trigger time relative to this batch's RunBlock call (from
        ps4000GetValuesTriggerTimeOffsetBulk64) — useful for detecting when
        one segment's "first pulse" is actually the validated echo of the
        previous segment, but only meaningful within this one batch (there is
        no absolute/wall-clock timestamp from the device).
        """
        ps = self._get_ps()
        timestamp = datetime.now()

        post_samples = config.num_samples - config.pre_trigger_samples
        timebase_index, actual_interval_ns = self.get_timebase(config)
        channel_enum = _CHANNEL_MAP[config.channel.upper()]

        # Cancel any leftover acquisition from a previous cycle or stale state,
        # same defensive call as run_block.
        ps.ps4000Stop(self._handle)

        # Reserve segmented memory and check the requested trace length fits.
        max_samples = ctypes.c_int32(0)
        status = ps.ps4000MemorySegments(
            self._handle, n_captures, ctypes.byref(max_samples)
        )
        self._check_status(status, "ps4000MemorySegments")
        if config.num_samples > max_samples.value:
            raise ValueError(
                f"config.num_samples ({config.num_samples}) exceeds the "
                f"{max_samples.value} samples/segment available when splitting "
                f"scope memory into {n_captures} segments. Reduce the capture "
                f"length or the number of captures per batch."
            )

        status = ps.ps4000SetNoOfCaptures(self._handle, n_captures)
        self._check_status(status, "ps4000SetNoOfCaptures")

        # Allocate one int16 buffer per segment and register each with the
        # driver via SetDataBufferBulk (the rapid-block counterpart to the
        # single-capture path's SetDataBuffers). All buffers must stay alive
        # until GetValuesBulk completes.
        buffers = [(ctypes.c_int16 * config.num_samples)() for _ in range(n_captures)]
        for segment_index, buffer in enumerate(buffers):
            status = ps.ps4000SetDataBufferBulk(
                self._handle,
                channel_enum,
                ctypes.byref(buffer),
                config.num_samples,
                segment_index,
            )
            self._check_status(status, f"ps4000SetDataBufferBulk(segment={segment_index})")

        # RunBlock — called once; the device fills all n_captures segments
        # from consecutive trigger events internally.
        time_indisposed = ctypes.c_int32(0)
        status = ps.ps4000RunBlock(
            self._handle,
            config.pre_trigger_samples,
            post_samples,
            timebase_index,
            1,                              # oversample = 1
            ctypes.byref(time_indisposed),
            0,                              # segmentIndex
            None,                           # lpReady callback (use polling)
            None,                           # pParameter
        )
        self._check_status(status, "ps4000RunBlock")

        # Poll until ready, bounded by a wall-clock deadline rather than a
        # fixed iteration count — a rare particle rate can leave some
        # segments untriggered indefinitely, and the batch must still be
        # salvageable rather than hanging forever.
        ready = ctypes.c_int16(0)
        deadline = time.monotonic() + ready_timeout_s
        partial_batch = False
        while True:
            status = ps.ps4000IsReady(self._handle, ctypes.byref(ready))
            self._check_status(status, "ps4000IsReady")
            if ready.value:
                break
            if time.monotonic() >= deadline:
                partial_batch = True
                break
            time.sleep(0.001)

        if partial_batch:
            ps.ps4000Stop(self._handle)
            n_completed = ctypes.c_uint16(0)
            status = ps.ps4000GetNoOfCaptures(self._handle, ctypes.byref(n_completed))
            self._check_status(status, "ps4000GetNoOfCaptures")
            if n_completed.value == 0:
                raise RuntimeError(
                    f"PicoScope: run_rapid_block timed out after {ready_timeout_s}s "
                    "waiting for ready; 0 of the requested captures completed."
                )
            n_captures = min(n_captures, n_completed.value)
            buffers = buffers[:n_captures]

        n_samples = ctypes.c_uint32(config.num_samples)
        overflow = (ctypes.c_int16 * n_captures)()
        status = ps.ps4000GetValuesBulk(
            self._handle,
            ctypes.byref(n_samples),
            0,                              # fromSegmentIndex
            n_captures - 1,                 # toSegmentIndex
            ctypes.byref(overflow),
        )
        self._check_status(status, "ps4000GetValuesBulk")

        # Per-segment trigger time, relative to this batch's RunBlock call —
        # lets the caller detect a segment whose "first pulse" is actually
        # the previously-validated echo re-triggering (see run_rapid_block's
        # docstring). Not available across separate run_rapid_block calls.
        trigger_times_raw = (ctypes.c_int64 * n_captures)()
        time_units = ctypes.c_int32(0)
        status = ps.ps4000GetValuesTriggerTimeOffsetBulk64(
            self._handle,
            ctypes.byref(trigger_times_raw),
            ctypes.byref(time_units),
            0,                              # fromSegmentIndex
            n_captures - 1,                 # toSegmentIndex
        )
        self._check_status(status, "ps4000GetValuesTriggerTimeOffsetBulk64")
        unit_to_ns = _TIME_UNIT_TO_NS.get(time_units.value, 1.0)
        trigger_offsets_ns = [t * unit_to_ns for t in trigger_times_raw]

        ps.ps4000Stop(self._handle)

        n = n_samples.value
        time_ns = np.arange(n, dtype=np.float64) * actual_interval_ns

        records: List[WaveformRecord] = []
        for segment_index, buffer in enumerate(buffers):
            raw = np.frombuffer(buffer, dtype=np.int16, count=n).astype(np.float64)

            voltage = raw / _ADC_MAX * config.voltage_range_v * config.probe_factor
            if config.invert_polarity:
                voltage = -voltage

            metadata: dict = {
                "actual_interval_ns": actual_interval_ns,
                "timebase_index": timebase_index,
                "overflow": bool(overflow[segment_index]),
                "n_values": n,
                "segment_index": segment_index,
                "trigger_offset_ns": trigger_offsets_ns[segment_index],
                "partial_batch": partial_batch,
            }

            records.append(WaveformRecord(
                trace_id=0,             # caller sets this
                run_id="",              # caller sets this
                timestamp=timestamp,
                voltage=voltage,
                time_ns=time_ns,
                sample_interval_ns=actual_interval_ns,
                config=config,
                metadata=metadata,
            ))

        return records

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_ps(self):
        """Lazy-load the picosdk ps4000 module."""
        if self._ps is None:
            import os, sys
            if sys.platform == "win32":
                # picosdk uses find_library("ps4000") which searches PATH, then
                # loads the DLL via WinDLL. ps4000.dll in turn loads picoipp.dll
                # dynamically at runtime (LoadLibrary by name), so that DLL also
                # needs to be reachable. We add each candidate directory to both
                # PATH (for find_library) and via os.add_dll_directory (for the
                # runtime LoadLibrary call on Python 3.8+).
                candidates = [
                    # PicoScope 7 install dir — contains a matched, tested set of
                    # ps4000.dll + picoipp.dll and is confirmed to talk to hardware.
                    r"C:\Program Files\Pico Technology\PicoScope 7 T&M Stable",
                    r"C:\Program Files\Pico Technology\SDK\lib",
                    r"C:\Program Files (x86)\Pico Technology\SDK\lib",
                ]
                for d in candidates:
                    if os.path.isdir(d):
                        if d not in os.environ.get("PATH", ""):
                            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
                        try:
                            os.add_dll_directory(d)
                        except OSError:
                            pass
            try:
                from picosdk.ps4000 import ps4000 as ps
                self._ps = ps
            except ImportError as exc:
                raise ImportError(
                    "picosdk is not installed. Run: pip install picosdk"
                ) from exc
        return self._ps

    @staticmethod
    def _timebase_to_ns(index: int) -> int:
        """Convert a ps4000 timebase index to sample interval in nanoseconds."""
        if index == 0:
            return 10
        if index == 1:
            return 20
        return (index - 2) * 8 + 32

    @staticmethod
    def _nearest_range_enum(voltage_range_v: float) -> int:
        """
        Return the ps4000 range enum that best covers the requested voltage range.
        Always rounds up to the next available range.
        """
        for v, enum in sorted(_RANGE_MAP.items()):
            if v >= voltage_range_v:
                return enum
        return max(_RANGE_MAP.values())  # largest available range

    @staticmethod
    def _check_status(status, operation: str) -> None:
        """Raise RuntimeError if an SDK call returned a non-zero status."""
        # picosdk wraps status codes; 0 = PICO_OK
        if hasattr(status, "value"):
            code = status.value
        else:
            code = int(status)
        if code != 0:
            raise RuntimeError(
                f"PicoScope SDK error in {operation!r}: status code {code:#010x}"
            )
