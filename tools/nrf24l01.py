"""nRF24L01+ — minimal SPI presence check and register read."""

import time
import spidev
import RPi.GPIO as GPIO

# Register addresses
_REG_CONFIG    = 0x00
_REG_EN_AA     = 0x01
_REG_EN_RXADDR = 0x02
_REG_SETUP_AW  = 0x03
_REG_RF_CH     = 0x05
_REG_RF_SETUP  = 0x06
_REG_STATUS    = 0x07
_REG_RX_ADDR_P0 = 0x0A
_REG_TX_ADDR   = 0x10
_REG_FIFO_STATUS = 0x17

# SPI commands
_CMD_R_REGISTER = 0x00
_CMD_W_REGISTER = 0x20
_CMD_NOP        = 0xFF

# CONFIG register defaults after power-on
_CONFIG_RESET   = 0x08   # PWR_UP=0, PRIM_RX=0, EN_CRC=1


class NRF24L01:
    """
    Minimal driver for the nRF24L01+ 2.4 GHz RF module.

    Sufficient for hardware-presence testing: verifies that the module
    responds on SPI and that readable registers hold sensible values.
    """

    def _sel_xfer(self, pkt):
        """One SPI transaction with this radio selected.

        With a kernel-owned chip-select (`cs_pin=None`) the SPI core asserts CS
        as part of the message, under the controller lock. Toggling CS from
        userspace instead leaves a window in which the kernel mcp251x driver
        can clock an MCP2515 message on the same controller while this device
        is already selected -- the mechanism that corrupted the DACs
        (IFS_HIL#124). The nRF24 carried the same exposure.

        CE and IRQ stay plain GPIO; only the chip-select moves.
        """
        if self._cs is None:
            return self._spi.xfer2(pkt)
        GPIO.output(self._cs, GPIO.LOW)
        try:
            return self._spi.xfer2(pkt)
        finally:
            GPIO.output(self._cs, GPIO.HIGH)

    def __init__(self, spi: spidev.SpiDev, cs_pin: int | None, ce_pin: int):
        """`cs_pin=None` means this radio has its own spi_device and the kernel
        owns its chip-select. CE is a plain GPIO either way -- it gates the
        radio, not the bus."""
        self._spi = spi
        self._cs = cs_pin
        self._ce = ce_pin
        # CE low = standby/idle
        GPIO.output(self._ce, GPIO.LOW)

    # ------------------------------------------------------------------
    # Low-level SPI helpers
    # ------------------------------------------------------------------

    def _read_reg(self, addr: int) -> int:
        resp = self._sel_xfer([_CMD_R_REGISTER | (addr & 0x1F), _CMD_NOP])
        return resp[1]

    def _write_reg(self, addr: int, value: int) -> None:
        self._sel_xfer([_CMD_W_REGISTER | (addr & 0x1F), value & 0xFF])

    def _nop(self) -> int:
        """Send NOP and return STATUS byte."""
        resp = self._sel_xfer([_CMD_NOP])
        return resp[0]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_present(self) -> bool:
        """
        Verify presence by writing and reading back a known value
        (RF_CH register, address 0x05, valid range 0-127).
        Returns True if the module responds correctly.
        """
        try:
            # Write a recognisable value to RF_CH
            self._write_reg(_REG_RF_CH, 0x4C)
            time.sleep(0.001)
            readback = self._read_reg(_REG_RF_CH)
            return readback == 0x4C
        except Exception:
            return False

    def read_status(self) -> int:
        """Return the STATUS register (received with every SPI command)."""
        return self._nop()

    def power_up(self) -> None:
        """Set PWR_UP bit in CONFIG register."""
        cfg = self._read_reg(_REG_CONFIG)
        self._write_reg(_REG_CONFIG, cfg | 0x02)
        time.sleep(0.002)   # tpd2stby ≤ 1.5 ms

    def power_down(self) -> None:
        """Clear PWR_UP bit — lowest power state."""
        cfg = self._read_reg(_REG_CONFIG)
        self._write_reg(_REG_CONFIG, cfg & ~0x02)

    def read_config(self) -> int:
        return self._read_reg(_REG_CONFIG)

    def set_channel(self, ch: int) -> None:
        """Set RF channel (0-127 = 2400–2527 MHz)."""
        self._write_reg(_REG_RF_CH, ch & 0x7F)

    def get_channel(self) -> int:
        return self._read_reg(_REG_RF_CH) & 0x7F

    def fifo_status(self) -> dict:
        raw = self._read_reg(_REG_FIFO_STATUS)
        return {
            "tx_reuse":  bool(raw & 0x40),
            "tx_full":   bool(raw & 0x20),
            "tx_empty":  bool(raw & 0x10),
            "rx_full":   bool(raw & 0x02),
            "rx_empty":  bool(raw & 0x01),
        }
