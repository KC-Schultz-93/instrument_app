"""
probe_config.py
----------------
Probe attenuation/gain factors and native PicoScope voltage ranges shared by
DAQPage and RatemeterPage's voltage-range UI.

No Qt, no hardware access, no file I/O — pure constants and a formatting
helper.

The scope's native hardware ranges (NATIVE_VOLTAGE_RANGES_V) are always what
actually gets sent to the PicoScope driver (see
services/picoscope_service.py._RANGE_MAP). A probe's factor converts between
that native, scope-input voltage and the true voltage at the probe tip:

    true_voltage = raw_scope_voltage * probe_factor

e.g. an "x0.1" probe/preamp applies ~10x gain before the signal reaches the
scope, so the true original signal is 1/10th of what the scope reads.
"""

PROBE_FACTORS = {
    "x0.1": 0.1,
    "x1": 1.0,
    "x10": 10.0,
}

# Native PicoScope 4262 hardware ranges this app exposes (volts).
NATIVE_VOLTAGE_RANGES_V = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]


def format_voltage_label(true_v: float) -> str:
    """Format a true (probe-scaled) voltage as "±X V" / "±X mV"."""
    if true_v >= 1.0:
        return f"±{true_v:g} V"
    return f"±{round(true_v * 1000.0, 6):g} mV"
