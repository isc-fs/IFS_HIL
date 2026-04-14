"""MCP2515 — SPI CAN controller driver (register-level, no kernel driver needed)."""

import time
import spidev
import RPi.GPIO as GPIO

# SPI instructions
_SPI_RESET      = 0xC0
_SPI_READ       = 0x03
_SPI_WRITE      = 0x02
_SPI_RTS        = 0x80   # Request-To-Send (|= buffer bits)
_SPI_READ_STATUS = 0xA0
_SPI_BIT_MODIFY = 0x05

# Register addresses
_CANSTAT  = 0x0E
_CANCTRL  = 0x0F
_TEC      = 0x1C
_REC      = 0x1D
_CNF3     = 0x28
_CNF2     = 0x29
_CNF1     = 0x2A
_CANINTE  = 0x2B
_CANINTF  = 0x2C
_EFLG     = 0x2D
_TXB0CTRL = 0x30
_TXB0SIDH = 0x31
_TXB0SIDL = 0x32
_TXB0DLC  = 0x35
_TXB0D0   = 0x36
_RXB0CTRL = 0x60
_RXB0SIDH = 0x61
_RXB0SIDL = 0x62
_RXB0DLC  = 0x65
_RXB0D0   = 0x66

# CANCTRL modes
_MODE_NORMAL    = 0x00
_MODE_SLEEP     = 0x20
_MODE_LOOPBACK  = 0x40
_MODE_LISTENONLY= 0x60
_MODE_CONFIG    = 0x80
_MODE_MASK      = 0xE0

# CANINTF flags
_INTF_RX0IF = 0x01
_INTF_TX0IF = 0x04


class MCP2515:
    """
    Minimal MCP2515 driver sufficient for hardware verification:
    - Chip reset and mode switching
    - Bitrate configuration (based on 16 MHz oscillator)
    - Loopback self-test (transmit + receive without external CAN bus)
    """

    # CNF1/CNF2/CNF3 for common bitrates with 16 MHz oscillator
    _BITRATE_CONFIG = {
        125_000:  (0x03, 0xF0, 0x86),  # CNF1, CNF2, CNF3
        250_000:  (0x01, 0xF0, 0x86),
        500_000:  (0x00, 0xF0, 0x86),
        1_000_000:(0x00, 0xD0, 0x82),
    }

    def __init__(self, spi: spidev.SpiDev, cs_pin: int, osc_hz: int = 16_000_000):
        self._spi = spi
        self._cs = cs_pin
        self._osc = osc_hz

    # ------------------------------------------------------------------
    # Low-level SPI helpers
    # ------------------------------------------------------------------

    def _select(self) -> None:
        GPIO.output(self._cs, GPIO.LOW)

    def _deselect(self) -> None:
        GPIO.output(self._cs, GPIO.HIGH)

    def _read_reg(self, addr: int) -> int:
        self._select()
        resp = self._spi.xfer2([_SPI_READ, addr, 0x00])
        self._deselect()
        return resp[2]

    def _write_reg(self, addr: int, data: int) -> None:
        self._select()
        self._spi.xfer2([_SPI_WRITE, addr, data & 0xFF])
        self._deselect()

    def _bit_modify(self, addr: int, mask: int, data: int) -> None:
        self._select()
        self._spi.xfer2([_SPI_BIT_MODIFY, addr, mask & 0xFF, data & 0xFF])
        self._deselect()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Send SPI RESET instruction and wait for config mode."""
        self._select()
        self._spi.xfer2([_SPI_RESET])
        self._deselect()
        time.sleep(0.01)

    def get_mode(self) -> int:
        """Return the current operating mode bits (upper 3 bits of CANSTAT)."""
        return self._read_reg(_CANSTAT) & _MODE_MASK

    def set_mode(self, mode: int, timeout: float = 0.1) -> bool:
        """Request a mode change and wait until confirmed. Returns True on success.

        If the chip is currently in sleep mode, a hardware reset is issued first
        to restart the oscillator before applying the requested mode.
        """
        if self.get_mode() == _MODE_SLEEP:
            self.reset()          # brings chip to CONFIG reliably (~10 ms)
            if mode == _MODE_CONFIG:
                return True
        self._bit_modify(_CANCTRL, _MODE_MASK, mode)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.get_mode() == mode:
                return True
            time.sleep(0.001)
        return False

    def configure_bitrate(self, bitrate: int) -> None:
        """Set CAN bitrate. Chip must be in CONFIG mode first."""
        try:
            cnf1, cnf2, cnf3 = self._BITRATE_CONFIG[bitrate]
        except KeyError:
            raise ValueError(f"Unsupported bitrate {bitrate}. "
                             f"Choose from {list(self._BITRATE_CONFIG)}")
        self._write_reg(_CNF1, cnf1)
        self._write_reg(_CNF2, cnf2)
        self._write_reg(_CNF3, cnf3)

    def init(self, bitrate: int = 500_000) -> bool:
        """
        Reset and configure the MCP2515 at the given bitrate.
        Returns True if the chip responds and enters config mode.
        """
        self.reset()
        if self.get_mode() != _MODE_CONFIG:
            return False
        self.configure_bitrate(bitrate)
        # Enable RX buffer 0, accept all messages
        self._write_reg(_RXB0CTRL, 0x60)
        return True

    def loopback_test(self, can_id: int = 0x123, data: bytes = b'\xDE\xAD\xBE\xEF') -> bool:
        """
        Enter loopback mode, transmit a frame, verify reception.
        Returns True if the transmitted frame is received intact.
        """
        if not self.set_mode(_MODE_LOOPBACK):
            return False

        # Load TX buffer 0
        sid_h = (can_id >> 3) & 0xFF
        sid_l = (can_id & 0x07) << 5
        dlc = len(data) & 0x0F
        self._write_reg(_TXB0SIDH, sid_h)
        self._write_reg(_TXB0SIDL, sid_l)
        self._write_reg(_TXB0DLC, dlc)
        for i, byte in enumerate(data):
            self._write_reg(_TXB0D0 + i, byte)

        # Clear RX interrupt flag and request TX
        self._bit_modify(_CANINTF, _INTF_RX0IF, 0x00)
        self._select()
        self._spi.xfer2([_SPI_RTS | 0x01])   # TXB0
        self._deselect()

        # Wait for RX
        deadline = time.time() + 0.1
        while time.time() < deadline:
            if self._read_reg(_CANINTF) & _INTF_RX0IF:
                break
            time.sleep(0.001)
        else:
            return False

        # Verify received ID
        rx_sidh = self._read_reg(_RXB0SIDH)
        rx_sidl = self._read_reg(_RXB0SIDL)
        rx_id = (rx_sidh << 3) | ((rx_sidl >> 5) & 0x07)
        if rx_id != can_id:
            return False

        # Verify received data
        rx_dlc = self._read_reg(_RXB0DLC) & 0x0F
        if rx_dlc != dlc:
            return False
        for i, expected in enumerate(data):
            if self._read_reg(_RXB0D0 + i) != expected:
                return False

        self._bit_modify(_CANINTF, _INTF_RX0IF, 0x00)
        return True

    def read_error_counters(self) -> tuple[int, int]:
        """Return (TEC, REC) error counters."""
        return self._read_reg(_TEC), self._read_reg(_REC)
