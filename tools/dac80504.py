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

# Expected Device ID value (lower 14 bits of DEVID register)
_DEVID_EXPECTED = 0x0295   # DAC80504 product ID

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

    # ------------------------------------------------------------------
    # Low-level register access
    # ------------------------------------------------------------------

    def _write_reg(self, addr: int, data: int) -> None:
        header = addr & 0x0F          # RW=0 (write), RESERVED=0, ADDR
        pkt = [header, (data >> 8) & 0xFF, data & 0xFF]
        GPIO.output(self._cs, GPIO.LOW)
        self._spi.xfer2(pkt)
        GPIO.output(self._cs, GPIO.HIGH)

    def _read_reg(self, addr: int) -> int:
        # DAC80504 SPI read: data is returned in the SAME 24-bit frame as the
        # read command (MISO valid during the read request transaction).
        # NOTE: The datasheet describes a pipelined two-frame protocol, but
        # hardware testing showed the pipelined approach (sending a NOOP after
        # the read command) returns 0x0000. Single-frame reads return correct
        # data. If readback still fails, check DAC MISO routing on the PCB.
        header = 0x80 | (addr & 0x0F)  # RW=1 (read)
        GPIO.output(self._cs, GPIO.LOW)
        resp = self._spi.xfer2([header, 0x00, 0x00])
        GPIO.output(self._cs, GPIO.HIGH)
        return (resp[1] << 8) | resp[2]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_device_id(self) -> int:
        """Read the DEVID register; expected lower 14 bits = 0x0295."""
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
