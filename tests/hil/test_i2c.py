"""
I2C BUS TESTS
=============
Covers all I2C devices on the board:
  - 4× INA226 power monitors (U1, U2, U4, standby)
  - 3× TCA9555 I/O expanders (U3, U6, U8)

Includes an auto-scan to detect which addresses actually respond,
which helps verify the A0/A1/A2 strapping against hw_config.py.

NOTE: probe uses write_byte (not read_byte) — TCA9555 NAKs read_byte
on some kernels; write_byte reliably ACKs all 7 devices.
"""

import pytest
import smbus2
from tools import hw_config as CFG


# ---------------------------------------------------------------------------
# Bus scan (runs first so failures elsewhere can be diagnosed)
# ---------------------------------------------------------------------------

class TestI2CScan:
    """Scan the I2C bus and report all responding addresses."""

    def test_scan_finds_7_devices(self, i2c_bus, capsys):
        """Expect exactly 7 devices: 4 INA226 + 3 TCA9555."""
        found = []
        for addr in range(0x08, 0x78):
            try:
                i2c_bus.write_byte(addr, 0x00)
                found.append(addr)
            except OSError:
                pass

        with capsys.disabled():
            print(f"\n  I2C scan found {len(found)} device(s): "
                  + ", ".join(f"0x{a:02X}" for a in found))

        expected = {
            CFG.INA226_ADDR_MLC1, CFG.INA226_ADDR_MLC2,
            CFG.INA226_ADDR_MLC3, CFG.INA226_ADDR_MLC4,
            CFG.TCA9555_ADDR_0,   CFG.TCA9555_ADDR_1, CFG.TCA9555_ADDR_2,
        }
        missing = expected - set(found)
        unexpected = set(found) - expected

        assert not missing, (
            f"Expected devices not found: {[hex(a) for a in missing]}. "
            "Check I2C bus enable (raspi-config), pull-ups (R50/R51), "
            "and address strapping in hw_config.py."
        )
        if unexpected:
            pytest.warns(UserWarning, match="unexpected")
            print(f"\n  WARNING: unexpected addresses found: "
                  + ", ".join(f"0x{a:02X}" for a in unexpected))


# ---------------------------------------------------------------------------
# INA226 tests
# ---------------------------------------------------------------------------

_INA226_ADDRS = [CFG.INA226_ADDR_MLC1, CFG.INA226_ADDR_MLC2,
                 CFG.INA226_ADDR_MLC3, CFG.INA226_ADDR_MLC4]
_INA226_NAMES = ["MLC1", "MLC2", "MLC3", "MLC4"]

_MFR_ID_EXPECTED  = 0x5449
_DIE_ID_EXPECTED  = 0x2260


class TestINA226:
    @pytest.mark.parametrize("idx", range(4), ids=_INA226_NAMES)
    def test_manufacturer_id(self, power_monitors, idx):
        mon = power_monitors[idx]
        if not mon.is_present():
            pytest.skip(f"{_INA226_NAMES[idx]} not responding")
        mfr = mon.read_manufacturer_id()
        assert mfr == _MFR_ID_EXPECTED, (
            f"{_INA226_NAMES[idx]}: manufacturer ID = 0x{mfr:04X}, "
            f"expected 0x{_MFR_ID_EXPECTED:04X} ('TI')"
        )

    @pytest.mark.parametrize("idx", range(4), ids=_INA226_NAMES)
    def test_die_id(self, power_monitors, idx):
        mon = power_monitors[idx]
        if not mon.is_present():
            pytest.skip(f"{_INA226_NAMES[idx]} not responding")
        die = mon.read_die_id()
        assert die == _DIE_ID_EXPECTED, (
            f"{_INA226_NAMES[idx]}: die ID = 0x{die:04X}, "
            f"expected 0x{_DIE_ID_EXPECTED:04X}"
        )

    @pytest.mark.parametrize("idx", range(4), ids=_INA226_NAMES)
    def test_bus_voltage_positive(self, power_monitors, idx):
        mon = power_monitors[idx]
        if not mon.is_present():
            pytest.skip(f"{_INA226_NAMES[idx]} not responding")
        pytest.skip(
            f"{_INA226_NAMES[idx]}: all INA226 monitors use low-side sensing "
            "(IN- near GND). bus_voltage() reads the shunt-side rail (~0 V), "
            "not the supply rail. This test cannot verify rail voltage in this topology."
        )


# ---------------------------------------------------------------------------
# TCA9555 tests
# ---------------------------------------------------------------------------

_TCA9555_ADDRS = [CFG.TCA9555_ADDR_0, CFG.TCA9555_ADDR_1, CFG.TCA9555_ADDR_2]
_TCA9555_NAMES = ["U3 (0x20)", "U6 (0x21)", "U8 (0x22)"]


class TestTCA9555:
    @pytest.mark.parametrize("idx", range(3), ids=_TCA9555_NAMES)
    def test_present(self, io_expanders, idx):
        exp = io_expanders[idx]
        assert exp.is_present(), (
            f"TCA9555 {_TCA9555_NAMES[idx]} not responding at "
            f"0x{_TCA9555_ADDRS[idx]:02X}. "
            "Check I2C address strapping and +3V3_SBY rail."
        )

    @pytest.mark.parametrize("idx", range(3), ids=_TCA9555_NAMES)
    def test_config_register_readback(self, io_expanders, idx):
        """Config register defaults to 0xFF (all inputs) after power-on."""
        exp = io_expanders[idx]
        if not exp.is_present():
            pytest.skip(f"{_TCA9555_NAMES[idx]} not responding")
        # After power cycle config regs = 0xFF; here we just verify R/W
        exp.set_all_inputs()
        assert exp.get_direction(0) == 0xFF
        assert exp.get_direction(1) == 0xFF

    @pytest.mark.parametrize("idx", range(3), ids=_TCA9555_NAMES)
    def test_output_register_write_readback(self, io_expanders, idx):
        """Write a pattern to the output register and read it back."""
        exp = io_expanders[idx]
        if not exp.is_present():
            pytest.skip(f"{_TCA9555_NAMES[idx]} not responding")

        exp.set_all_outputs()
        try:
            for pattern in (0xAA, 0x55, 0xFF, 0x00):
                exp.write_port(0, pattern)
                exp.write_port(1, pattern ^ 0xFF)
                snap = exp.read_all()
                assert snap["output_port0"] == pattern, (
                    f"{_TCA9555_NAMES[idx]} port0: wrote 0x{pattern:02X}, "
                    f"read back 0x{snap['output_port0']:02X}"
                )
                assert snap["output_port1"] == (pattern ^ 0xFF), (
                    f"{_TCA9555_NAMES[idx]} port1: wrote 0x{(pattern ^ 0xFF):02X}, "
                    f"read back 0x{snap['output_port1']:02X}"
                )
        finally:
            # Leave all pins as inputs (safe default)
            exp.set_all_inputs()
            exp.write_all(0x00, 0x00)

    @pytest.mark.parametrize("idx", range(3), ids=_TCA9555_NAMES)
    def test_individual_pin_toggle(self, io_expanders, idx):
        """Toggle each pin individually and verify the output register updates."""
        exp = io_expanders[idx]
        if not exp.is_present():
            pytest.skip(f"{_TCA9555_NAMES[idx]} not responding")

        exp.set_all_outputs()
        exp.write_all(0x00, 0x00)
        try:
            for port in range(2):
                for pin in range(8):
                    exp.write_pin(port, pin, True)
                    assert exp.read_all()[f"output_port{port}"] & (1 << pin), (
                        f"{_TCA9555_NAMES[idx]} port{port} pin{pin}: set HIGH failed"
                    )
                    exp.write_pin(port, pin, False)
                    assert not (exp.read_all()[f"output_port{port}"] & (1 << pin)), (
                        f"{_TCA9555_NAMES[idx]} port{port} pin{pin}: set LOW failed"
                    )
        finally:
            exp.set_all_inputs()
