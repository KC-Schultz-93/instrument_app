"""
test_cdms_analyzer.py
---------------------
Unit tests for CDMSAnalyzer physics calculations. No hardware required.

Run with:
    python Tests/test_cdms_analyzer.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from Services.CDMSAnalyzer import CDMSAnalyzer
from Services.DAQModels import CDMSConfig


# ---------------------------------------------------------------------------
# Charge estimation tests
# ---------------------------------------------------------------------------

def test_charge_known_sample():
    """
    Reference point from user: a ~9000e sample should produce ~5.76 mV
    with the CoolFET amplifier (cal factor 0.64 µV/e).
    Invert: Q = 5.76 mV / 0.64 µV/e = 9000 e.
    """
    cfg = CDMSConfig(charge_cal_uv_per_e=0.64)
    analyzer = CDMSAnalyzer(cfg)
    result = analyzer.estimate_charge_e(5.76e-3)  # 5.76 mV
    assert abs(result - 9000) < 1, (
        f"Charge estimate {result:.1f} e, expected 9000 e"
    )
    print(f"  charge (5.76 mV):  {result:.1f} e  (expected 9000 e)  - PASS")


def test_charge_zero_amplitude():
    """Zero amplitude should give zero charge."""
    analyzer = CDMSAnalyzer(CDMSConfig())
    result = analyzer.estimate_charge_e(0.0)
    assert result == 0.0
    print("  charge (0 V):  0.0 e  - PASS")


def test_charge_linearity():
    """Doubling the amplitude should double the charge."""
    analyzer = CDMSAnalyzer(CDMSConfig(charge_cal_uv_per_e=0.64))
    q1 = analyzer.estimate_charge_e(1e-3)
    q2 = analyzer.estimate_charge_e(2e-3)
    assert abs(q2 / q1 - 2.0) < 1e-6, f"Linearity broken: {q1:.1f} / {q2:.1f}"
    print(f"  charge linearity:  {q1:.0f} e -> {q2:.0f} e  - PASS")


def test_charge_custom_cal_factor():
    """A custom calibration factor should scale the result correctly."""
    analyzer = CDMSAnalyzer(CDMSConfig(charge_cal_uv_per_e=1.0))
    result = analyzer.estimate_charge_e(1e-3)  # 1 mV = 1000 µV
    assert abs(result - 1000.0) < 0.1
    print(f"  charge (custom cal):  {result:.1f} e  - PASS")


# ---------------------------------------------------------------------------
# m/z tests
# ---------------------------------------------------------------------------

def test_mz_uncalibrated_returns_none():
    """When trap_k == 0, compute_mz should return None."""
    analyzer = CDMSAnalyzer(CDMSConfig(trap_k_Da_Hz2=0.0))
    result = analyzer.compute_mz(1e6)
    assert result is None, f"Expected None, got {result}"
    print("  mz (uncalibrated):  None  - PASS")


def test_mz_zero_freq_returns_none():
    """Zero frequency should return None even when K is set."""
    analyzer = CDMSAnalyzer(CDMSConfig(trap_k_Da_Hz2=1e9))
    result = analyzer.compute_mz(0.0)
    assert result is None
    print("  mz (zero freq):  None  - PASS")


def test_mz_formula():
    """m/z = K / f² should hold for a known example.

    K = 1e13 Da·Hz², f = 100 kHz → m/z = 1e13 / (1e5)² = 1e13 / 1e10 = 1000 Da/e
    """
    K = 1e13       # Da·Hz²
    f = 100_000.0  # 100 kHz
    expected_mz = K / (f ** 2)  # = 1000 Da/e

    analyzer = CDMSAnalyzer(CDMSConfig(trap_k_Da_Hz2=K))
    result = analyzer.compute_mz(f)

    assert result is not None
    assert abs(result - expected_mz) < 1e-6, (
        f"m/z {result:.3f} Da/e, expected {expected_mz:.3f} Da/e"
    )
    print(f"  mz formula:  {result:.2f} Da/e  (expected {expected_mz:.2f})  - PASS")


# ---------------------------------------------------------------------------
# Mass tests
# ---------------------------------------------------------------------------

def test_mass_formula():
    """mass [Da] = charge_e [e] × mz [Da/e]."""
    analyzer = CDMSAnalyzer(CDMSConfig())
    mass = analyzer.compute_mass_Da(9000.0, 1000.0)  # 9000e × 1000 Da/e
    assert abs(mass - 9_000_000.0) < 1, f"Mass {mass:.0f} Da, expected 9 MDa"
    print(f"  mass formula:  {mass / 1e6:.1f} MDa  - PASS")


# ---------------------------------------------------------------------------
# Full analyse() integration
# ---------------------------------------------------------------------------

def test_analyse_no_calibration():
    """analyse() with default (uncalibrated) K should return mz=None, mass=None."""
    analyzer = CDMSAnalyzer(CDMSConfig())
    result = analyzer.analyse(dominant_freq_hz=50_000.0, mean_peak_height_v=5.76e-3)
    assert result.mz_Da_per_e is None
    assert result.mass_Da is None
    assert abs(result.charge_e - 9000.0) < 1
    print(f"  analyse (no K):  charge={result.charge_e:.0f} e  mz=None  mass=None  - PASS")


def test_analyse_with_calibration():
    """analyse() with K set should compute mz and mass.

    K = 1e13 Da·Hz², f = 100 kHz → m/z = 1000 Da/e; charge = 9000 e → mass = 9 MDa
    """
    K = 1e13
    f = 100_000.0
    analyzer = CDMSAnalyzer(CDMSConfig(charge_cal_uv_per_e=0.64, trap_k_Da_Hz2=K))
    result = analyzer.analyse(dominant_freq_hz=f, mean_peak_height_v=5.76e-3)
    assert result.mz_Da_per_e is not None
    assert result.mass_Da is not None
    assert abs(result.mz_Da_per_e - 1000.0) < 1e-3
    expected_mass = 9000.0 * 1000.0  # charge_e * mz
    assert abs(result.mass_Da - expected_mass) < 1.0
    print(
        f"  analyse (with K):  charge={result.charge_e:.0f} e  "
        f"mz={result.mz_Da_per_e:.1f} Da/e  mass={result.mass_Da:.0f} Da  - PASS"
    )


def test_analyse_no_peaks():
    """analyse() with no peaks (None amplitude) should not raise."""
    analyzer = CDMSAnalyzer(CDMSConfig())
    result = analyzer.analyse(dominant_freq_hz=0.0, mean_peak_height_v=None)
    assert result.charge_e == 0.0
    assert result.mz_Da_per_e is None
    print("  analyse (no peaks):  charge=0  mz=None  - PASS")


if __name__ == "__main__":
    print("CDMSAnalyzer tests (no hardware required)")
    print("=" * 50)
    test_charge_known_sample()
    test_charge_zero_amplitude()
    test_charge_linearity()
    test_charge_custom_cal_factor()
    test_mz_uncalibrated_returns_none()
    test_mz_zero_freq_returns_none()
    test_mz_formula()
    test_mass_formula()
    test_analyse_no_calibration()
    test_analyse_with_calibration()
    test_analyse_no_peaks()
    print()
    print("All CDMSAnalyzer tests passed.")
