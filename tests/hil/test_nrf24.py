"""
nRF24L01 TESTS  —  U23 RF breakout module
==========================================
Tests:
  1. Module is present: write/read-back RF_CH register confirms SPI comms.
  2. Power-up sequence works (PWR_UP bit in CONFIG).
  3. RF channel can be set and read back correctly across the full range.
  4. FIFO STATUS register reads a sensible value (TX/RX empty after reset).
  5. Module powers down cleanly.

NOTE: No transmission test is performed here — that requires a second
      nRF24L01 node. This test only verifies the module on the PCB.
"""

import time
import pytest
from tools import hw_config as CFG


class TestNRF24Presence:
    """Verify the nRF24L01 responds on SPI."""

    def test_spi_present(self, nrf24):
        assert nrf24.is_present(), (
            "nRF24L01 not responding on SPI. "
            f"Check CS_pin=GPIO{CFG.NRF24_CS} and CE_pin=GPIO{CFG.NRF24_CE}. "
            "Verify +3V3_pi rail is present and the module is seated correctly."
        )


class TestNRF24PowerUp:
    """Verify power-up and power-down sequences."""

    def test_power_up(self, nrf24):
        nrf24.power_down()
        time.sleep(0.002)
        nrf24.power_up()
        cfg = nrf24.read_config()
        assert cfg & 0x02, (
            f"PWR_UP bit not set in CONFIG after power_up() — CONFIG = 0x{cfg:02X}"
        )

    def test_power_down(self, nrf24):
        nrf24.power_up()
        time.sleep(0.002)
        nrf24.power_down()
        cfg = nrf24.read_config()
        assert not (cfg & 0x02), (
            f"PWR_UP bit still set after power_down() — CONFIG = 0x{cfg:02X}"
        )


class TestNRF24Registers:
    """Read/write register checks."""

    def test_rf_channel_sweep(self, nrf24):
        """Write and read back channels 0, 63, 127."""
        for ch in (0, 40, 63, 100, 127):
            nrf24.set_channel(ch)
            readback = nrf24.get_channel()
            assert readback == ch, (
                f"RF_CH: wrote {ch}, read back {readback}"
            )

    def test_fifo_status_after_reset(self, nrf24):
        """After power-on, TX and RX FIFOs should both be empty."""
        # Power cycle to ensure clean state
        nrf24.power_down()
        time.sleep(0.005)
        nrf24.power_up()
        time.sleep(0.002)

        fifo = nrf24.fifo_status()
        assert fifo["tx_empty"], (
            f"TX FIFO not empty after reset: {fifo}"
        )
        assert fifo["rx_empty"], (
            f"RX FIFO not empty after reset: {fifo}"
        )
        assert not fifo["tx_full"], (
            f"TX FIFO reports FULL after reset: {fifo}"
        )

    def test_status_register_readable(self, nrf24):
        """STATUS register (returned on every SPI transaction) must be non-0xFF."""
        status = nrf24.read_status()
        assert status != 0xFF, (
            "STATUS register returned 0xFF — module may not be connected or "
            "SPI MISO is floating. Check nRF24 power and SPI wiring."
        )
        assert status != 0x00, (
            "STATUS register returned 0x00 — unexpected for powered module."
        )


class TestNRF24Cleanup:
    """Leave module in a safe low-power state."""

    def test_power_down_at_end(self, nrf24):
        nrf24.power_down()
        cfg = nrf24.read_config()
        assert not (cfg & 0x02), "Module not powered down at end of test suite"
