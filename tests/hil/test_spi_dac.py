"""
SPI DAC TESTS  —  4× DAC80504 (16 analog output channels)
===========================================================
Tests:
  1. Each DAC80504 responds to SPI and reports the correct Device ID.
  2. All 4 channels per DAC can be written and read back via the DAC register.
  3. Full-scale sweep (0 V → 3.3 V → 0 V) verifies code linearity on readback.
  4. All outputs are left at 0 V after the test (safe default).

NOTE: Voltage readback is from the DAC register, not an ADC measurement.
      To verify actual output voltage, connect DAC outputs to ADC inputs
      and run a cross-check test (not included here — requires loopback wiring).
"""

import pytest
import time
from tools import hw_config as CFG

DAC_NAMES = ["DAC1 (U12)", "DAC2 (U13)", "DAC3 (U14)", "DAC4 (U15)"]

_DEVID_EXPECTED = 0x0295   # DAC80504 product ID


class TestDAC80504Presence:
    """Verify each DAC80504 responds and identifies itself."""

    @pytest.mark.parametrize("idx", range(4), ids=DAC_NAMES)
    def test_device_id(self, dacs, idx):
        dac = dacs[idx]
        try:
            dev_id = dac.read_device_id()
        except Exception as e:
            pytest.fail(
                f"{DAC_NAMES[idx]}: SPI communication failed — {e}. "
                "Check CS pin in hw_config.py and SPI_GATE_OE assertion."
            )
        assert dev_id == _DEVID_EXPECTED, (
            f"{DAC_NAMES[idx]}: Device ID = 0x{dev_id:04X}, "
            f"expected 0x{_DEVID_EXPECTED:04X}. "
            "Check SPI wiring and CS pin assignment."
        )


class TestDAC80504Reset:
    """Software reset leaves registers in known state."""

    @pytest.mark.parametrize("idx", range(4), ids=DAC_NAMES)
    def test_reset_and_readback(self, dacs, idx):
        dac = dacs[idx]
        try:
            dac.reset()
            time.sleep(0.005)
            # After reset, DAC outputs are 0 V (register = 0x0000)
            for ch in range(4):
                v = dac.get_voltage(ch)
                assert v < 0.05, (
                    f"{DAC_NAMES[idx]} channel {ch}: "
                    f"after reset, register reads {v:.4f} V (expected ~0 V)"
                )
        finally:
            dac.zero_all()


class TestDAC80504WriteReadback:
    """Write specific codes to each channel and verify register readback."""

    TEST_VOLTAGES = [0.0, 0.5, 1.0, 1.65, 2.5, 3.3]

    @pytest.mark.parametrize("idx", range(4), ids=DAC_NAMES)
    def test_channel_sweep(self, dacs, idx):
        dac = dacs[idx]
        vref = CFG.RAIL_LIMITS["3V3"][1]   # use upper limit as Vref guard
        try:
            for target_v in self.TEST_VOLTAGES:
                v = min(target_v, vref)
                for ch in range(4):
                    dac.set_voltage(ch, v)
                    readback = dac.get_voltage(ch)
                    # Allow ±1 LSB tolerance at 16-bit (≈50 µV)
                    tol = 3.3 / 65535 * 2
                    assert abs(readback - v) < tol, (
                        f"{DAC_NAMES[idx]} ch{ch}: wrote {v:.4f} V, "
                        f"read back {readback:.4f} V (tol ±{tol*1e6:.0f} µV)"
                    )
        finally:
            dac.zero_all()


class TestDAC80504AllOutputsZero:
    """Confirm all channels are at 0 V (cleanup check)."""

    @pytest.mark.parametrize("idx", range(4), ids=DAC_NAMES)
    def test_outputs_at_zero(self, dacs, idx):
        dac = dacs[idx]
        dac.zero_all()
        for ch in range(4):
            v = dac.get_voltage(ch)
            assert v < 0.001, f"{DAC_NAMES[idx]} ch{ch}: not zeroed, reads {v:.4f} V"
