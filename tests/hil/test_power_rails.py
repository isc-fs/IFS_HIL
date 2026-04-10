"""
POWER RAIL TESTS
================
Verify that all three ATX power rails (+12 V, +5 V, +3.3 V) are within ±5 %
of their nominal voltage as measured by the INA226 monitors.

Expected behaviour:
  - All three INA226 chips respond on I2C and report valid Device/Mfr IDs.
  - Bus voltages are within the limits defined in hw_config.RAIL_LIMITS.
  - No overcurrent condition is present on any rail.
"""

import pytest
from tools import hw_config as CFG


RAIL_NAMES = ["12V", "5V", "3V3"]


class TestINA226Presence:
    """Verify each INA226 is reachable and identifies itself correctly."""

    @pytest.mark.parametrize("idx,name", list(enumerate(RAIL_NAMES)))
    def test_ina226_present(self, power_monitors, idx, name):
        mon = power_monitors[idx]
        assert mon.is_present(), (
            f"INA226 for {name} rail not responding at "
            f"I2C address 0x{[CFG.INA226_ADDR_12V, CFG.INA226_ADDR_5V, CFG.INA226_ADDR_3V3][idx]:02X}. "
            "Check address strapping (A0/A1 pins) and I2C pull-ups."
        )


class TestVoltageRails:
    """Verify measured bus voltages are within ±5 % of nominal."""

    @pytest.mark.parametrize("idx,name", list(enumerate(RAIL_NAMES)))
    def test_voltage_in_range(self, power_monitors, idx, name):
        mon = power_monitors[idx]
        if not mon.is_present():
            pytest.skip(f"INA226 for {name} not present — skipping voltage check")

        v = mon.bus_voltage()
        lo, hi = CFG.RAIL_LIMITS[name]
        assert lo <= v <= hi, (
            f"{name} rail: measured {v:.3f} V, expected {lo:.2f}–{hi:.2f} V. "
            "Check ATX PSU is powered and PS_ON# is asserted."
        )

    @pytest.mark.parametrize("idx,name", list(enumerate(RAIL_NAMES)))
    def test_current_not_negative(self, power_monitors, idx, name):
        """Current should be non-negative (sinking from PSU, not injecting)."""
        mon = power_monitors[idx]
        if not mon.is_present():
            pytest.skip(f"INA226 for {name} not present")

        i = mon.current()
        # Allow a small negative threshold for ADC noise around zero
        assert i >= -0.05, (
            f"{name} rail: unexpected negative current {i:.4f} A. "
            "Check shunt orientation and INA226 calibration."
        )


class TestPowerSnapshot:
    """Print a full power snapshot for every rail (informational)."""

    @pytest.mark.parametrize("idx,name", list(enumerate(RAIL_NAMES)))
    def test_print_snapshot(self, power_monitors, idx, name, capsys):
        mon = power_monitors[idx]
        if not mon.is_present():
            pytest.skip(f"INA226 for {name} not present")

        snap = mon.snapshot()
        with capsys.disabled():
            print(f"\n  [{name}]  "
                  f"Vbus={snap['bus_voltage_V']:.3f} V  "
                  f"I={snap['current_A']*1000:.1f} mA  "
                  f"P={snap['power_W']*1000:.1f} mW")
        # No assert — this is just for observation
