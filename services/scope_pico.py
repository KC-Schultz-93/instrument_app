# instrument_app/services/scope_pico.py
"""
###########################      MAY NOT NEED FOR MRI

PicoScopeService: PS4000A-series ingest for the CDMS page.

- Rapid Block mode: one capture per event (repeat forever).
- Emits (np.int16 array, fs_hz) via block_ready for the Analyzer.
- Streaming mode: placeholder (emits a status line and returns).

Usage:
    pico = PicoScopeService(channel="A", vrange=2.0, coupling="DC",
                            fs_hz=2_400_000.0, n_samples=262_144,
                            trigger_level_v=0.05, pretrigger_frac=0.05)
    pico.block_ready.connect(analyzer.analyze_block)

Notes:
- Requires PicoSDK C libraries + `picosdk` Python wrappers.
- API: PS4000A Programmer's Guide (timebase, channels, trigger, buffers).
"""

from __future__ import annotations

from ctypes import byref, c_int16, c_int32, c_uint32, c_float, POINTER
from typing import Optional, Tuple
import numpy as np

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

# PicoSDK imports
from picosdk.ps4000a import ps4000a as ps
from picosdk.functions import assert_pico_ok, mV2adc


class PicoError(RuntimeError):
    pass


class PicoScopeService(QObject):
    """
    Qt-friendly wrapper around PS4000A driver.

    Signals:
        block_ready(np.ndarray[int16], float): event block + sample rate (Hz)
        status(str): human-readable status messages

    Slots:
        start_rapid_block()
        start_streaming()
        stop()
    """
    block_ready = pyqtSignal(object, float)
    status = pyqtSignal(str)

    def __init__(
        self,
        *,
        channel: str = "A",
        vrange: float = 2.0,          # ±2 V full scale
        coupling: str = "DC",         # "AC" or "DC"
        fs_hz: float = 2_400_000.0,   # target sample rate
        n_samples: int = 262_144,     # points per event
        trigger_level_v: float = 0.05,# absolute volts, rising edge
        pretrigger_frac: float = 0.05 # % of samples captured before trigger
    ):
        super().__init__()
        self._running = False
        self._h = c_int16()             # device handle
        self._open = False

        self._ch = self._ch_enum(channel)
        self._vr = self._range_enum(vrange)
        self._vr_fs_volts = float(vrange)  # for threshold conversion
        self._cpl = self._coupling_enum(coupling)

        self._fs = float(fs_hz)
        self._N = int(n_samples)
        self._pre_frac = float(pretrigger_frac)
        self._trig_v = float(trigger_level_v)

        # cached max ADC value (device-specific)
        self._max_adc = c_int16(32767)

    # --------------------- Public Slots --------------------- #

    @pyqtSlot()
    def start_rapid_block(self):
        """
        Triggered, one-capture-per-iteration loop.
        Good for “one file per event” workflows.
        """
        try:
            self._ensure_open()
            self._configure_channel()
            self._query_max_adc()
            self._configure_trigger_rising(self._trig_v)

            timebase = self._choose_timebase(self._fs, self._N)
            pre = int(self._N * self._pre_frac)
            post = self._N - pre

            # One reusable buffer on the selected channel
            buf = (c_int16 * self._N)()
            # segmentIndex=0, no downsampling (RATIO_MODE_NONE)
            status = ps.ps4000aSetDataBuffer(self._h, self._ch, byref(buf), self._N, 0,
                                             ps.PS4000A_RATIO_MODE["PS4000A_RATIO_MODE_NONE"])
            assert_pico_ok(status)

            # Main acquisition loop
            self._running = True
            self.status.emit(f"Pico Rapid: fs≈{self._fs:,.0f} Hz, N={self._N}, pre={pre}, timebase={timebase}")
            while self._running:
                # Start a single block
                time_indisposed_ms = c_int32()
                status = ps.ps4000aRunBlock(self._h, pre, post, timebase, None, 0, None, None)
                assert_pico_ok(status)

                # Wait for ready
                ready = c_int16(0)
                while self._running and not ready.value:
                    status = ps.ps4000aIsReady(self._h, byref(ready))
                    assert_pico_ok(status)
                    QThread.msleep(1)

                if not self._running:
                    break

                # Retrieve into buf
                n_captured = c_uint32(self._N)
                overflow = c_int16(0)
                status = ps.ps4000aGetValues(
                    self._h,
                    0,                      # startIndex
                    byref(n_captured),
                    1,                      # downSampleRatio
                    ps.PS4000A_RATIO_MODE["PS4000A_RATIO_MODE_NONE"],
                    0,                      # segmentIndex
                    byref(overflow),
                )
                assert_pico_ok(status)

                # Convert ctypes buffer -> numpy int16 (copy)
                data = np.frombuffer(buf, dtype=np.int16, count=int(n_captured.value)).copy()
                # Emit to analyzer
                self.block_ready.emit(data, self._fs)

                # Stop the scope (required between blocks)
                ps.ps4000aStop(self._h)

            self.status.emit("Pico Rapid: stopped.")

        except Exception as e:
            self.status.emit(f"Pico Rapid: error: {e!r}")
        finally:
            self._cleanup()

    @pyqtSlot()
    def start_streaming(self):
        """
        Placeholder. We’ll fill this once Rapid is validated on your setup.
        For now it just posts a status line and exits cleanly.
        """
        self.status.emit("Pico Streaming: not implemented yet. Use Rapid Block for now.")
        # No-op: return immediately so UI stays responsive.

    @pyqtSlot()
    def stop(self):
        self._running = False
        # driver stop is called inside the loop; closing happens in _cleanup()

    # --------------------- Driver helpers --------------------- #

    def _ensure_open(self):
        if self._open:
            return
        status = ps.ps4000aOpenUnit(byref(self._h), None)
        assert_pico_ok(status)
        self._open = True
        self.status.emit("Pico: device opened")

    def _configure_channel(self):
        status = ps.ps4000aSetChannel(
            self._h,
            self._ch,
            1,  # enabled
            self._cpl,
            self._vr,
            0.0  # analog offset volts
        )
        assert_pico_ok(status)

    def _configure_trigger_rising(self, level_v: float):
        # Convert volts → mV → ADC counts for the selected range
        # (mV2adc expects max_adc code and range enum)
        thresh_adc = mV2adc(int(level_v * 1000), self._vr, self._max_adc)
        status = ps.ps4000aSetSimpleTrigger(
            self._h,
            1,  # enabled
            self._ch,
            int(thresh_adc),
            ps.PS4000A_THRESHOLD_DIRECTION["PS4000A_RISING"],
            0,      # delay
            0       # autoTrigger ms (0 = wait forever)
        )
        assert_pico_ok(status)

    def _query_max_adc(self):
        max_adc = c_int16()
        status = ps.ps4000aMaximumValue(self._h, byref(max_adc))
        assert_pico_ok(status)
        self._max_adc = max_adc

    def _choose_timebase(self, fs_hz: float, n_samples: int) -> int:
        """
        Pick the nearest timebase index that yields a sample interval close to 1/fs.
        The driver returns (timeIntervalNs, maxSamples) for a (timebase, n_samples).
        """
        target_ns = 1e9 / fs_hz
        best_tb, best_err = 2, float("inf")
        ti_ns = c_float()
        maxs = c_int32()

        # Scan a modest window; PS4000A timebase is monotonic
        for tb in range(1, 10_000):
            status = ps.ps4000aGetTimebase2(self._h, tb, n_samples, byref(ti_ns), byref(maxs), 0)
            if status != 0:
                continue  # skip invalid combos
            err = abs(ti_ns.value - target_ns)
            if err < best_err:
                best_tb, best_err = tb, err
                if err / target_ns < 0.02:  # within 2% is fine
                    break
        if best_err == float("inf"):
            raise PicoError("Could not find a valid timebase for the requested fs.")
        # Update actual fs with the chosen timebase (good to be honest downstream)
        self._fs = 1e9 / float(ti_ns.value)
        return best_tb

    def _cleanup(self):
        try:
            if self._open:
                try:
                    ps.ps4000aStop(self._h)
                except Exception:
                    pass
                ps.ps4000aCloseUnit(self._h)
                self.status.emit("Pico: device closed")
        finally:
            self._open = False

    # --------------------- Enum helpers --------------------- #

    @staticmethod
    def _ch_enum(ch: str) -> int:
        ch = (ch or "A").strip().upper()
        key = f"PS4000A_CHANNEL_{ch}"
        return ps.PS4000A_CHANNEL[key]

    @staticmethod
    def _coupling_enum(cpl: str) -> int:
        cpl = (cpl or "DC").strip().upper()
        key = f"PS4000A_{cpl}"
        return ps.PS4000A_COUPLING[key]

    @staticmethod
    def _range_enum(vrange: float) -> int:
        """
        Map ±volts full-scale to the nearest PS4000A range.
        Common ranges: 10mV, 20mV, 50mV, 100mV, 200mV, 500mV, 1V, 2V, 5V, 10V, 20V
        """
        opts = [
            (0.01, "PS4000A_10MV"),
            (0.02, "PS4000A_20MV"),
            (0.05, "PS4000A_50MV"),
            (0.1,  "PS4000A_100MV"),
            (0.2,  "PS4000A_200MV"),
            (0.5,  "PS4000A_500MV"),
            (1.0,  "PS4000A_1V"),
            (2.0,  "PS4000A_2V"),
            (5.0,  "PS4000A_5V"),
            (10.0, "PS4000A_10V"),
            (20.0, "PS4000A_20V"),
        ]
        vr = float(vrange)
        best = min(opts, key=lambda p: abs(p[0] - vr))
        return ps.PS4000A_RANGE[best[1]]
# --------------------- End of file --------------------- #