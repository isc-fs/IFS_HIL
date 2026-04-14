"""DAC80504 — 4-channel 16-bit SPI DAC driver."""

import spidev
import RPi.GPIO as GPIO

# Register addresses
_REG_NOOP     = 0x00
_REG_DEVID    = 0x01
_REG_SYNC     = 0x02
_REG_CONFIG   = 0x03
_REG_GAIN     = 0x04
_REG_TRIGGER  = 0x05
_REG_BRDCAST  = 0x06
_REG_STATUS   = 0x07
_REG_DAC_A    = 0x08
_REG_DAC_B    = 0x09
_REG_DAC_C    = 0x0A
_REG_DAC_D    = 0x0B

# Expected Device ID value (lower 14 bits of DEVID register).
# Confirmed by direct read in SPI mode 1: bits [13:2] = 0x105, version = 3.
_DEVID_EXPECTED = 0x0417   # DAC80504 product ID (rev 3)

_DAC_REGS = [_REG_DAC_A, _REG_DAC_B, _REG_DAC_C, _REG_DAC_D]


class DAC80504:
    """
    Driver for the Texas Instruments DAC80504 quad 16-bit DAC.

    SPI frame: 24 bits = 8-bit header + 16-bit data.
    Header: RW(1) | RESERVED(3) | ADDR(4)
    Data: 16-bit register value.

    Board configuration (REFDIV=GND, GAIN=GND, REF=+3V3):
      Output range 0 – 3.3 V, LDAC async (low), gain = ×1.
    """

    def __init__(self, spi: spidev.SpiDev, cs_pin: int, vref: float = 3.3):
        self._spi = spi
        self._cs = cs_pin
        self._vref = vref
        # Power down the internal 2.5 V reference.  The board supplies an
        # external 3.3 V reference on VREF.  With REF-PWR-DOWN = 0 (reset
        # default) the internal reference is active and contests the external
        # source on the same pin, setting STATUS.REF_ALM and making outputs
        # unpredictable.  CONFIG bit 8 = 1 disconnects the internal reference.
        self._write_reg(_REG_CONFIG, 0x0100)
        # Enable asynchronous updates for all channels so that writes to DAC
        # registers take effect immediately.  The reset default is 0x000F
        # (synchronous), which requires an LDAC edge to latch the output —
        # LDAC is tied LOW on this board so the edge never occurs.
        self._write_reg(_REG_SYNC, 0x0000)
        # Configure GAIN register for 0–VREF output range.
        # REF-DIV (bit 8) = 1: the internal reference divider is active, so the
        # reference reaching the output stage is VREF/2.  Setting BUFF-GAIN = 1
        # (×2) for all four channels restores full-scale: 2×(VREF/2)×D/65536 =
        # VREF×D/65536.  Reset default 0x0000 leaves BUFF-GAIN=0 (×1) which
        # caps the output at VREF/2 ≈ 1.65 V.
        self._write_reg(_REG_GAIN, 0x010F)

    # ------------------------------------------------------------------
    # Low-level register access
    # ------------------------------------------------------------------

    def _xfer(self, pkt: list) -> list:
        """
        Execute a 24-bit SPI transaction in mode 1 (CPOL=0, CPHA=1).

        The DAC80504 drives SDO on the rising edge of SCLK; the master must
        therefore sample on the falling edge — that is SPI mode 1.  The shared
        spi_bus is opened in mode 0 for the CAN and ADC devices; we switch
        mode around each DAC transaction and restore it afterwards.
        """
        old_mode = self._spi.mode
        self._spi.mode = 0b01          # mode 1: CPOL=0, CPHA=1
        GPIO.output(self._cs, GPIO.LOW)
        resp = self._spi.xfer2(pkt)
        GPIO.output(self._cs, GPIO.HIGH)
        self._spi.mode = old_mode
        return resp

    def _write_reg(self, addr: int, data: int) -> None:
        header = addr & 0x0F          # RW=0 (write), RESERVED=0, ADDR
        self._xfer([header, (data >> 8) & 0xFF, data & 0xFF])

    def _read_reg(self, addr: int) -> int:
        # DAC80504 uses a pipelined SPI read: send a READ command in frame N,
        # then send a NOP in frame N+1; MISO during frame N+1 carries the data.
        # CS is deasserted between frames (each frame is a separate transaction).
        # This requires mode 1 so MISO is sampled on the falling edge of SCLK,
        # one full half-period after the DAC drives SDO on the rising edge.
        header = 0x80 | (addr & 0x0F)  # RW=1 (read)
        self._xfer([header, 0x00, 0x00])          # frame N: issue read command
        resp = self._xfer([_REG_NOOP, 0x00, 0x00])  # frame N+1: NOP, capture data
        return (resp[1] << 8) | resp[2]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_device_id(self) -> int:
        """Read the DEVID register; expected lower 14 bits = 0x0417."""
        return self._read_reg(_REG_DEVID) & 0x3FFF

    def reset(self) -> None:
        """Perform a software reset via the TRIGGER register."""
        self._write_reg(_REG_TRIGGER, 0x000A)

    def set_voltage(self, channel: int, voltage: float) -> None:
        """
        Set *channel* (0=A, 1=B, 2=C, 3=D) output to *voltage* (Volts).
        Clamps to [0, vref].
        """
        if channel < 0 or channel > 3:
            raise ValueError(f"Channel must be 0-3, got {channel}")
        v = max(0.0, min(voltage, self._vref))
        code = int(v / self._vref * 65535)
        self._write_reg(_DAC_REGS[channel], code)

    def set_all(self, voltage: float) -> None:
        """Set all four channels to the same voltage."""
        for ch in range(4):
            self.set_voltage(ch, voltage)

    def zero_all(self) -> None:
        """Drive all channels to 0 V (safe default)."""
        self.set_all(0.0)

    def get_voltage(self, channel: int) -> float:
        """Read back the DAC register and return the set voltage."""
        if channel < 0 or channel > 3:
            raise ValueError(f"Channel must be 0-3, got {channel}")
        raw = self._read_reg(_DAC_REGS[channel])
        return raw / 65535.0 * self._vref
