"""
CDMSAnalyzer.py
---------------
Physics calculations for Charge Detection Mass Spectrometry.

All methods are instance methods but stateless relative to individual traces â€”
the only state held is the CDMSConfig calibration parameters for the run.

Charge model (CoolFET amplifier):
    Q [e] = V_peak [Î¼V] / charge_cal_uv_per_e

m/z model (simple harmonic oscillator, to be refined with SIMION):
    m/z [Da/e] = trap_k_Da_Hz2 / f [Hz]Â²
    K_trap = 0 means uncalibrated â€” m/z and mass are returned as None.

Mass:
    mass [Da] = charge_e [e] Ã— (m/z [Da/e])
"""

from __future__ import annotations

from typing import Optional

from instrument_app.services.daq_models import CDMSConfig, CDMSResult


class CDMSAnalyzer:
    """
    Converts per-trace signal measurements into physical CDMS quantities.

    Parameters
    ----------
    config : CDMSConfig
        Calibration parameters for this run.
    """

    def __init__(self, config: CDMSConfig) -> None:
        self._cfg = config

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def estimate_charge_e(self, peak_amplitude_v: float) -> float:
        """
        Estimate charge in elementary charges from peak signal amplitude.

        Uses the CoolFET amplifier calibration:
            Q [e] = (amplitude [V] Ã— 1e6) / charge_cal_uv_per_e

        Parameters
        ----------
        peak_amplitude_v : float
            Mean peak height above baseline in volts.

        Returns
        -------
        float
            Estimated charge in elementary charges (e).
        """
        if self._cfg.charge_cal_uv_per_e <= 0:
            return 0.0
        amplitude_uv = peak_amplitude_v * 1e6
        return amplitude_uv / self._cfg.charge_cal_uv_per_e

    def compute_mz(self, freq_hz: float) -> Optional[float]:
        """
        Compute m/z in Daltons per elementary charge from oscillation frequency.

        Uses the simple harmonic trap model:
            m/z [Da/e] = K_trap [DaÂ·HzÂ²] / f [Hz]Â²

        Returns None when K_trap is 0 (uncalibrated) or freq_hz is zero/negative.
        K_trap should be updated after SIMION calibration.

        Parameters
        ----------
        freq_hz : float
            Dominant oscillation frequency in Hz.

        Returns
        -------
        float or None
        """
        if self._cfg.trap_k_Da_Hz2 == 0.0 or freq_hz <= 0.0:
            return None
        return self._cfg.trap_k_Da_Hz2 / (freq_hz ** 2)

    def compute_mass_Da(self, charge_e: float, mz_Da_per_e: float) -> float:
        """
        Compute mass in Daltons.

            mass [Da] = charge_e [e] Ã— (m/z [Da/e])

        Parameters
        ----------
        charge_e : float
            Charge estimate in elementary charges.
        mz_Da_per_e : float
            m/z ratio in Da/e.

        Returns
        -------
        float
            Mass in Daltons.
        """
        return charge_e * mz_Da_per_e

    def analyse(
        self,
        dominant_freq_hz: float,
        mean_peak_height_v: Optional[float],
    ) -> CDMSResult:
        """
        Run the full per-trace CDMS physics calculation and return a CDMSResult.

        Parameters
        ----------
        dominant_freq_hz : float
            Dominant frequency from the FFT of the baseline-corrected waveform.
        mean_peak_height_v : float or None
            Mean peak amplitude above baseline in volts; None if no peaks found.

        Returns
        -------
        CDMSResult
        """
        amp_v = mean_peak_height_v if mean_peak_height_v is not None else 0.0
        charge_e = self.estimate_charge_e(amp_v)
        mz = self.compute_mz(dominant_freq_hz)
        mass = self.compute_mass_Da(charge_e, mz) if mz is not None else None

        return CDMSResult(
            oscillation_freq_hz=dominant_freq_hz,
            charge_e=charge_e,
            mz_Da_per_e=mz,
            mass_Da=mass,
        )
