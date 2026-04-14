"""
MLC CARRIER POWER MONITOR TESTS  —  4× INA226 (low-side current sensing)
=========================================================================
The BACKPLANE_HIL has one INA226 per MLC carrier slot. Each monitor sits
in the power feed to the carrier (STM32 board), measuring how much current
and power that slot draws from the supply.

Wiring topology: low-side current sensing (IN- near GND).
bus_voltage() therefore reads ~0 V by design — it reflects the shunt-side
node voltage, not the supply rail. Only current and power are meaningful.

Tests:
  1. Each INA226 responds on I2C and identifies itself (Mfr/Die ID).
  2. Measured current is non-negative (sourcing to the carrier, not back-feeding).
  3. Current is below the configured maximum threshold.
  4. Snapshot printed for observation.
"""

import pytest
from tools import hw_config as CFG


MLC_NAMES = ["MLC1", "MLC2", "MLC3", "MLC4"]
_MLC_ADDRS = [CFG.INA226_ADDR_MLC1, CFG.INA226_ADDR_MLC2,
              CFG.INA226_ADDR_MLC3, CFG.INA226_ADDR_MLC4]


class TestMLCMonitorPresence:
    """Verify each INA226 is reachable."""

    @pytest.mark.parametrize("idx,name", list(enumerate(MLC_NAMES)))
    def test_ina226_present(self, power_monitors, idx, name):
        mon = power_monitors[idx]
        assert mon.is_present(), (
            f"INA226 for {name} not responding at "
            f"I2C address 0x{_MLC_ADDRS[idx]:02X}. "
            "Check address strapping (A0/A1 pins) and I2C pull-ups."
        )


class TestMLCCurrentMeasurement:
    """Verify carrier current readings are sensible."""

    @pytest.mark.parametrize("idx,name", list(enumerate(MLC_NAMES)))
    def test_current_not_negative(self, power_monitors, idx, name):
        """Current must be >= 0 — carriers draw from supply, not back-feed."""
        mon = power_monitors[idx]
        if not mon.is_present():
            pytest.skip(f"{name} INA226 not present")

        i = mon.current()
        assert i >= -0.05, (
            f"{name}: unexpected negative current {i:.4f} A. "
            "Check shunt orientation and INA226 calibration."
        )

    @pytest.mark.parametrize("idx,name", list(enumerate(MLC_NAMES)))
    def test_current_below_max(self, power_monitors, idx, name):
        """Current must be below the configured maximum (no carrier plugged in → near 0)."""
        mon = power_monitors[idx]
        if not mon.is_present():
            pytest.skip(f"{name} INA226 not present")

        i = mon.current()
        assert i < CFG.MLC_CURRENT_MAX_A, (
            f"{name}: current {i:.3f} A exceeds limit {CFG.MLC_CURRENT_MAX_A} A. "
            "Check carrier for short circuit or unexpected load."
        )


class TestMLCPowerSnapshot:
    """Print current and power for every MLC slot (informational)."""

    @pytest.mark.parametrize("idx,name", list(enumerate(MLC_NAMES)))
    def test_print_snapshot(self, power_monitors, idx, name, capsys):
        mon = power_monitors[idx]
        if not mon.is_present():
            pytest.skip(f"{name} INA226 not present")

        snap = mon.snapshot()
        with capsys.disabled():
            print(f"\n  [{name}]  "
                  f"I={snap['current_A']*1000:.2f} mA  "
                  f"P={snap['power_W']*1000:.2f} mW  "
                  f"Vshunt={snap['shunt_voltage_V']*1000:.3f} mV")
