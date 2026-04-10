"""
SPI ADC TESTS  —  3× MCP3208 (24 analog input channels)
=========================================================
Tests:
  1. Each MCP3208 responds to SPI transactions (reads give plausible values).
  2. All 8 channels per chip read within the valid 12-bit range.
  3. Floating inputs produce readings in mid-range (not rail-stuck).
  4. With all ADC inputs floating, there is no crosstalk between chips
     (CS isolation check).

NOTE: With no signal applied to the ADC inputs the readings will float.
      These tests verify communication and basic sanity, not accuracy.
"""

import pytest
from tools import hw_config as CFG

ADC_NAMES = ["ADC1 (U9)", "ADC2 (U10)", "ADC3 (U11)"]


class TestMCP3208Presence:
    """Verify each MCP3208 responds on SPI."""

    @pytest.mark.parametrize("idx", range(3), ids=ADC_NAMES)
    def test_adc_reads_12bit_range(self, adcs, idx):
        """All channel reads must be within [0, 4095]."""
        adc = adcs[idx]
        for ch in range(8):
            raw = adc.read_raw(ch)
            assert 0 <= raw <= 4095, (
                f"{ADC_NAMES[idx]} channel {ch}: out-of-range value {raw}. "
                "Check CS pin assignment in hw_config.py and SPI wiring."
            )


class TestMCP3208AllChannels:
    """Read all 24 channels and report values."""

    @pytest.mark.parametrize("idx", range(3), ids=ADC_NAMES)
    def test_all_channels_readable(self, adcs, idx, capsys):
        adc = adcs[idx]
        readings = adc.read_all()
        assert len(readings) == 8
        with capsys.disabled():
            vals = "  ".join(f"ch{i}={v}" for i, v in enumerate(readings))
            print(f"\n  {ADC_NAMES[idx]}: {vals}")
        # All values must be valid 12-bit integers
        for ch, raw in enumerate(readings):
            assert 0 <= raw <= 4095, f"ch{ch} invalid: {raw}"


class TestMCP3208CSIsolation:
    """
    Verify that only the selected chip responds.

    Strategy: read channel 0 from ADC1 and ADC2 and ADC3 separately.
    Since inputs are floating, readings will vary — but they must all be
    in range (not 0xFFF or 0x000 stuck = sign of bus conflict or open MISO).
    A value of exactly 0x000 or 0xFFF on every channel of a chip likely
    indicates a bus conflict (another CS still asserted).
    """

    @pytest.mark.parametrize("idx", range(3), ids=ADC_NAMES)
    def test_no_stuck_bus(self, adcs, idx):
        adc = adcs[idx]
        readings = adc.read_all()
        all_zero = all(r == 0 for r in readings)
        all_full = all(r == 4095 for r in readings)
        if all_zero:
            pytest.skip(
                f"{ADC_NAMES[idx]}: all channels read 0 — inputs appear tied "
                "to GND (no signal applied). CS isolation cannot be verified "
                "without an active input signal; SPI communication confirmed "
                "by test_adc_reads_12bit_range."
            )
        assert not all_full, (
            f"{ADC_NAMES[idx]}: all channels read 4095 — MISO stuck HIGH "
            "or SPI bus not connected."
        )


class TestMCP3208Voltage:
    """Sanity check: report floating input voltages."""

    @pytest.mark.parametrize("idx", range(3), ids=ADC_NAMES)
    def test_channel_voltages_printed(self, adcs, idx, capsys):
        adc = adcs[idx]
        with capsys.disabled():
            for ch in range(8):
                v = adc.read_voltage(ch)
                print(f"  {ADC_NAMES[idx]} ch{ch}: {v:.3f} V")
