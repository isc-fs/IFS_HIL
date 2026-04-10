"""INA226 — I2C bidirectional current/power monitor driver."""

import smbus2
import struct

# Register map
_REG_CONFIG       = 0x00
_REG_SHUNT_V      = 0x01
_REG_BUS_V        = 0x02
_REG_POWER        = 0x03
_REG_CURRENT      = 0x04
_REG_CALIBRATION  = 0x05
_REG_MASK_ENABLE  = 0x06
_REG_ALERT_LIMIT  = 0x07
_REG_MFR_ID       = 0xFE
_REG_DIE_ID       = 0xFF

_MFR_ID_EXPECTED  = 0x5449   # "TI"
_DIE_ID_EXPECTED  = 0x2260   # INA226

# Default config: avg=16 samples, Vbus CT=1.1 ms, Vsh CT=1.1 ms, continuous
_CONFIG_DEFAULT = 0x4527


class INA226:
    """
    Driver for the Texas Instruments INA226 power monitor.

    Measures bus voltage, shunt voltage, current, and power.
    All values are returned in SI units (V, A, W).

    shunt_ohm: shunt resistor value in Ohms (e.g. 0.01 for 10 mΩ).
    max_current_A: expected maximum current; sets calibration register.
    """

    # LSBs per datasheet
    _VBUS_LSB    = 1.25e-3   # 1.25 mV per bit
    _VSHUNT_LSB  = 2.5e-6    # 2.5 µV per bit

    def __init__(self, bus: smbus2.SMBus, addr: int,
                 shunt_ohm: float = 0.01, max_current_A: float = 3.0):
        self._bus = bus
        self._addr = addr
        self._shunt_ohm = shunt_ohm
        self._current_lsb = max_current_A / 32768.0
        self._configure()

    # ------------------------------------------------------------------
    # Register I/O
    # ------------------------------------------------------------------

    def _read_reg(self, reg: int) -> int:
        raw = self._bus.read_i2c_block_data(self._addr, reg, 2)
        return struct.unpack(">H", bytes(raw))[0]

    def _write_reg(self, reg: int, value: int) -> None:
        data = [(value >> 8) & 0xFF, value & 0xFF]
        self._bus.write_i2c_block_data(self._addr, reg, data)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _configure(self) -> None:
        self._write_reg(_REG_CONFIG, _CONFIG_DEFAULT)
        cal = int(0.00512 / (self._current_lsb * self._shunt_ohm))
        self._write_reg(_REG_CALIBRATION, cal & 0x7FFF)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_manufacturer_id(self) -> int:
        return self._read_reg(_REG_MFR_ID)

    def read_die_id(self) -> int:
        return self._read_reg(_REG_DIE_ID)

    def is_present(self) -> bool:
        """Return True if manufacturer and die IDs match expected values."""
        try:
            return (self.read_manufacturer_id() == _MFR_ID_EXPECTED and
                    self.read_die_id() == _DIE_ID_EXPECTED)
        except OSError:
            return False

    def bus_voltage(self) -> float:
        """Return bus voltage in Volts."""
        raw = self._read_reg(_REG_BUS_V)
        return raw * self._VBUS_LSB

    def shunt_voltage(self) -> float:
        """Return shunt voltage in Volts (signed)."""
        raw = self._read_reg(_REG_SHUNT_V)
        # 2's complement for negative values
        if raw > 0x7FFF:
            raw -= 0x10000
        return raw * self._VSHUNT_LSB

    def current(self) -> float:
        """Return current in Amperes (signed)."""
        raw = self._read_reg(_REG_CURRENT)
        if raw > 0x7FFF:
            raw -= 0x10000
        return raw * self._current_lsb

    def power(self) -> float:
        """Return power in Watts."""
        raw = self._read_reg(_REG_POWER)
        return raw * 25.0 * self._current_lsb

    def snapshot(self) -> dict:
        """Return a dict with all measurements."""
        return {
            "bus_voltage_V":   self.bus_voltage(),
            "shunt_voltage_V": self.shunt_voltage(),
            "current_A":       self.current(),
            "power_W":         self.power(),
        }
