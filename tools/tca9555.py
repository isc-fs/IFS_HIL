"""TCA9555 — 16-bit I2C I/O expander driver."""

import smbus2

# Register map (port 0 = IO0_x, port 1 = IO1_x)
_REG_INPUT_0  = 0x00
_REG_INPUT_1  = 0x01
_REG_OUTPUT_0 = 0x02
_REG_OUTPUT_1 = 0x03
_REG_POL_0    = 0x04
_REG_POL_1    = 0x05
_REG_CONFIG_0 = 0x06
_REG_CONFIG_1 = 0x07


class TCA9555:
    """
    Driver for the Texas Instruments TCA9555 16-bit I/O expander.

    Two 8-bit ports (port 0 and port 1), each pin independently
    configurable as input (1) or output (0) via the config registers.

    All methods that read/write a port use 0 = port 0, 1 = port 1.
    Pin values are plain bit masks (0x00 – 0xFF per port).
    """

    def __init__(self, bus: smbus2.SMBus, addr: int):
        self._bus = bus
        self._addr = addr

    # ------------------------------------------------------------------
    # Register I/O
    # ------------------------------------------------------------------

    def _read(self, reg: int) -> int:
        return self._bus.read_byte_data(self._addr, reg)

    def _write(self, reg: int, value: int) -> None:
        self._bus.write_byte_data(self._addr, reg, value & 0xFF)

    # ------------------------------------------------------------------
    # Direction control
    # ------------------------------------------------------------------

    def set_direction(self, port: int, mask: int) -> None:
        """
        Set pin direction for *port* (0 or 1).
        *mask* bits: 1 = input, 0 = output.
        """
        reg = _REG_CONFIG_0 if port == 0 else _REG_CONFIG_1
        self._write(reg, mask)

    def get_direction(self, port: int) -> int:
        reg = _REG_CONFIG_0 if port == 0 else _REG_CONFIG_1
        return self._read(reg)

    def set_all_outputs(self) -> None:
        """Configure all 16 pins as outputs."""
        self._write(_REG_CONFIG_0, 0x00)
        self._write(_REG_CONFIG_1, 0x00)

    def set_all_inputs(self) -> None:
        """Configure all 16 pins as inputs."""
        self._write(_REG_CONFIG_0, 0xFF)
        self._write(_REG_CONFIG_1, 0xFF)

    # ------------------------------------------------------------------
    # Output control
    # ------------------------------------------------------------------

    def write_port(self, port: int, value: int) -> None:
        """Write *value* to all output pins of *port* (0 or 1)."""
        reg = _REG_OUTPUT_0 if port == 0 else _REG_OUTPUT_1
        self._write(reg, value)

    def write_pin(self, port: int, pin: int, state: bool) -> None:
        """Set or clear a single pin (0-7) on *port*."""
        reg = _REG_OUTPUT_0 if port == 0 else _REG_OUTPUT_1
        current = self._read(reg)
        if state:
            current |= (1 << pin)
        else:
            current &= ~(1 << pin)
        self._write(reg, current)

    def write_all(self, port0_val: int, port1_val: int) -> None:
        """Write both ports in one call."""
        self._write(_REG_OUTPUT_0, port0_val)
        self._write(_REG_OUTPUT_1, port1_val)

    # ------------------------------------------------------------------
    # Input readback
    # ------------------------------------------------------------------

    def read_port(self, port: int) -> int:
        """Read the current logic state of all pins on *port*."""
        reg = _REG_INPUT_0 if port == 0 else _REG_INPUT_1
        return self._read(reg)

    def read_pin(self, port: int, pin: int) -> bool:
        """Read a single pin state."""
        return bool(self.read_port(port) & (1 << pin))

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def is_present(self) -> bool:
        """Return True if the chip ACKs on the I2C bus."""
        try:
            self._read(_REG_CONFIG_0)
            return True
        except OSError:
            return False

    def read_all(self) -> dict:
        """Return a snapshot of both input ports and both config registers."""
        return {
            "input_port0":  self._read(_REG_INPUT_0),
            "input_port1":  self._read(_REG_INPUT_1),
            "output_port0": self._read(_REG_OUTPUT_0),
            "output_port1": self._read(_REG_OUTPUT_1),
            "config_port0": self._read(_REG_CONFIG_0),
            "config_port1": self._read(_REG_CONFIG_1),
        }
