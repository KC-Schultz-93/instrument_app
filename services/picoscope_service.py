"""
PicoScopeService.py
-------------------
All communication with the PicoScope 4262 via the ps4000 driver family.
This is the only module that imports or calls picosdk.

No Qt. No file I/O. All methods are synchronous and intended to be called
from a worker thread (run_block is a blocking call).

Hardware notes:
  - ADC max count for ps4000 series: 32512 (not 32768)
  - Timebase 0 â†’ 10 ns, 1 â†’ 20 ns, n â‰¥ 2 â†’ (n-2)*8 + 32 ns
  - ps4000 uses int16 buffers; SetDataBuffer must be called before GetValues
  - The ctypes buffer must remain alive until GetValues completes
"""

from __future__ import annotations

import ctypes
import time
from datetime import datetime
from typing import Tuple

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

# Trigger direction (THRESHOLD_DIRECTION)
_DIRECTION_MAP = {
    "ABOVE":          0,
    "BELOW":          1,
    "RISING":         2,
    "FALLING":        3,
    "RISING_OR_FALLING": 4,
}

# ADC full-scale count for ps4000 series
_ADC_MAX = 32512


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
        status = ps.open_unit(ctypes.byref(self._handle))
        self._check_status(status, "open_unit")
        self._connected = True

    def disconnect(self) -> None:
        """Close the device handle. Safe to call when not connected."""
        if not self._connected:
            return
        ps = self._get_ps()
        ps.close_unit(self._handle)
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

        # Disable all channels first, then enable the requested one.
        for ch in range(4):
            status = ps.set_channel(
                self._handle,
                ctypes.c_int32(ch),
                ctypes.c_int16(1 if ch == channel_enum else 0),  # enabled
                ctypes.c_int32(coupling_enum if ch == channel_enum else 1),
                ctypes.c_int32(range_enum if ch == channel_enum else 7),
            )
            self._check_status(status, f"set_channel(ch={ch})")

    def get_timebase(self, config: AcquisitionConfig) -> Tuple[int, int]:
        """
        Find the ps4000 timebase index that best matches config.sample_interval_ns.

        Returns:
            (timebase_index, actual_interval_ns)

        ps4000 timebase mapping:
            0 â†’ 10 ns
            1 â†’ 20 ns
            n â‰¥ 2 â†’ (n-2)*8 + 32 ns
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
        """Configure a simple edge trigger, or disable triggering."""
        ps = self._get_ps()

        if not config.trigger_enabled:
            # Disable trigger by setting enabled=0
            status = ps.set_simple_trigger(
                self._handle,
                ctypes.c_int16(0),          # enabled = false
                ctypes.c_int32(0),          # source (ignored)
                ctypes.c_int16(0),          # threshold (ignored)
                ctypes.c_int32(2),          # direction RISING (ignored)
                ctypes.c_uint32(0),         # delay
                ctypes.c_int16(0),          # autoTrigger_ms (0 = wait forever)
            )
            self._check_status(status, "set_simple_trigger(disabled)")
            return

        threshold_v = config.trigger_threshold_v or 0.0
        threshold_adc = int(threshold_v / config.voltage_range_v * _ADC_MAX)
        threshold_adc = max(-_ADC_MAX, min(_ADC_MAX, threshold_adc))

        direction_enum = _DIRECTION_MAP.get(config.trigger_direction.upper(), 2)
        channel_enum = _CHANNEL_MAP.get(config.channel.upper(), 0)

        status = ps.set_simple_trigger(
            self._handle,
            ctypes.c_int16(1),                          # enabled
            ctypes.c_int32(channel_enum),               # source channel
            ctypes.c_int16(threshold_adc),              # threshold in ADC counts
            ctypes.c_int32(direction_enum),             # direction
            ctypes.c_uint32(0),                         # delay (samples)
            ctypes.c_int16(auto_trigger_ms),             # autoTrigger_ms
        )
        self._check_status(status, "set_simple_trigger")

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def run_block(self, config: AcquisitionConfig) -> WaveformRecord:
        """
        Run one block acquisition. BLOCKING â€” call from a worker thread.

        Steps:
          1. Determine timebase
          2. ps4000RunBlock
          3. Poll ps4000IsReady (yields GIL via time.sleep)
          4. ps4000SetDataBuffer + ps4000GetValues
          5. ADC â†’ volts conversion
          6. Return WaveformRecord

        The ctypes buffer is kept alive in a local variable until GetValues
        completes to prevent premature garbage collection.
        """
        ps = self._get_ps()
        timestamp = datetime.now()

        post_samples = config.num_samples - config.pre_trigger_samples
        timebase_index, actual_interval_ns = self.get_timebase(config)

        # RunBlock
        time_indisposed = ctypes.c_int32(0)
        status = ps.run_block(
            self._handle,
            ctypes.c_int32(config.pre_trigger_samples),
            ctypes.c_int32(post_samples),
            ctypes.c_uint32(timebase_index),
            ctypes.c_int16(1),              # oversample = 1
            ctypes.byref(time_indisposed),
            ctypes.c_uint32(0),             # segmentIndex
            None,                           # lpReady callback (use polling)
            None,                           # pParameter
        )
        self._check_status(status, "run_block")

        # Poll until ready
        ready = ctypes.c_int16(0)
        for _ in range(100_000):
            status = ps.is_ready(self._handle, ctypes.byref(ready))
            self._check_status(status, "is_ready")
            if ready.value:
                break
            time.sleep(0.001)
        else:
            raise RuntimeError("PicoScope: run_block timed out waiting for ready.")

        # Allocate int16 buffer â€” keep reference alive until GetValues returns
        channel_enum = _CHANNEL_MAP[config.channel.upper()]
        buffer = (ctypes.c_int16 * config.num_samples)()

        status = ps.set_data_buffer(
            self._handle,
            ctypes.c_int32(channel_enum),
            ctypes.byref(buffer),
            ctypes.c_int32(config.num_samples),
        )
        self._check_status(status, "set_data_buffer")

        overflow = ctypes.c_int16(0)
        n_values = ctypes.c_int32(config.num_samples)
        status = ps.get_values(
            self._handle,
            ctypes.c_uint32(0),             # startIndex
            ctypes.byref(n_values),
            ctypes.c_uint32(1),             # downSampleRatio
            ctypes.c_int32(0),             # downSampleRatioMode = NONE
            ctypes.c_uint32(0),             # segmentIndex
            ctypes.byref(overflow),
        )
        self._check_status(status, "get_values")

        n = n_values.value
        raw = np.frombuffer(buffer, dtype=np.int16, count=n).astype(np.float64)

        # ADC â†’ volts
        voltage = raw / _ADC_MAX * config.voltage_range_v
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_ps(self):
        """Lazy-load the picosdk ps4000 module."""
        if self._ps is None:
            try:
                from picosdk.ps4000 import ps4000 as ps
                self._ps = ps
            except ImportError as exc:
                raise ImportError(
                    "picosdk-python-wrappers is not installed. "
                    "Run: pip install picosdk-python-wrappers"
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
