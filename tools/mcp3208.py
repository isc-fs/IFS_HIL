"""MCP3208 — 8-channel 12-bit SPI ADC driver."""

import spidev
import RPi.GPIO as GPIO


class MCP3208:
    """
    Driver for the Microchip MCP3208 8-channel 12-bit ADC.

    Uses software chip-select (any GPIO) over a shared SPI bus.
    Vref is assumed to match the supply voltage provided at init time.

    SPI mode: CPOL=0, CPHA=0 (mode 0,0), max 2 MHz.
    """

    def __init__(self, spi: spidev.SpiDev, cs_pin: int, vref: float = 3.3):
        self._spi = spi
        self._cs = cs_pin
        self._vref = vref

    def read_raw(self, channel: int, differential: bool = False) -> int:
        """
        Read raw 12-bit value from the given channel (0-7).

        If *differential* is True, reads the differential pair
        (channel, channel+1) — channel must be even.
        Returns a value in [0, 4095].
        """
        if channel < 0 or channel > 7:
            raise ValueError(f"Channel must be 0-7, got {channel}")

        # MCP3208 protocol: send 3 bytes, receive 3 bytes.
        # Byte 0: start bit (1) + SGL/~DIFF + D2
        # Byte 1: D1 D0 + 6 don't-care
        # Byte 2: don't-care (clock out result)
        sgl = 0 if differential else 1
        cmd = [
            0x04 | (sgl << 1) | ((channel >> 2) & 0x01),
            (channel & 0x03) << 6,
            0x00,
        ]

        GPIO.output(self._cs, GPIO.LOW)
        response = self._spi.xfer2(cmd)
        GPIO.output(self._cs, GPIO.HIGH)

        # Result: response[1] bits[3:0] (12-bit MSB) + response[2] (8-bit LSB)
        raw = ((response[1] & 0x0F) << 8) | response[2]
        return raw

    def read_voltage(self, channel: int) -> float:
        """Return the voltage on *channel* in Volts."""
        return self.read_raw(channel) / 4095.0 * self._vref

    def read_all(self) -> list[int]:
        """Return raw readings for all 8 channels."""
        return [self.read_raw(ch) for ch in range(8)]
