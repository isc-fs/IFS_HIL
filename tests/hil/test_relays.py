"""
RELAY TESTS  —  4× RT314A12 SPDT relays (K1–K4)
=================================================
Relays are controlled via TCA9555 I/O expander outputs that drive the
NMOS gate driver transistors (Q1–Q4).

Tests:
  1. Each relay coil can be energised and de-energised via the TCA9555 pin.
  2. No relay activates when a different relay is energised (isolation check).
  3. All relays are left de-energised at the end of the test.

NOTE: These tests do not verify the relay contact state (NO/NC switching).
      To do that, run the relay self-test with a continuity tester or connect
      the switched outputs back through an ADC channel.

SAFETY: Relay coils run on +12 V. Ensure the ATX PSU is powered before
        running this test (otherwise the coil current will be zero and
        the relays will not actuate — the test will still pass because
        we only verify the TCA9555 output register, not the physical contact).
"""

import time
import pytest
from tools import hw_config as CFG
from tools.hil_client import TCA9555

RELAY_NAMES = ["K1", "K2", "K3", "K4"]


def _relay_expander(io_expanders, relay_name: str) -> TCA9555:
    addr, port, bit = CFG.RELAY_PINS[relay_name]
    idx = [CFG.TCA9555_ADDR_0, CFG.TCA9555_ADDR_1, CFG.TCA9555_ADDR_2].index(addr)
    return io_expanders[idx]


def _energise(io_expanders, relay_name: str, state: bool) -> None:
    addr, port, bit = CFG.RELAY_PINS[relay_name]
    exp = _relay_expander(io_expanders, relay_name)
    exp.write_pin(port, bit, state)
    time.sleep(0.02)   # relay settling time (~10 ms typical)


class TestRelayControl:
    """Energise and de-energise each relay, verify TCA9555 output register."""

    @pytest.fixture(autouse=True)
    def _deenergise_all(self, io_expanders):
        """Ensure all relay driver pins are low before and after each test."""
        for name in RELAY_NAMES:
            try:
                addr, port, bit = CFG.RELAY_PINS[name]
                exp = _relay_expander(io_expanders, name)
                if exp.is_present():
                    exp.set_all_outputs()
                    exp.write_pin(port, bit, False)
            except Exception:
                pass
        yield
        for name in RELAY_NAMES:
            try:
                addr, port, bit = CFG.RELAY_PINS[name]
                exp = _relay_expander(io_expanders, name)
                if exp.is_present():
                    exp.write_pin(port, bit, False)
            except Exception:
                pass

    @pytest.mark.parametrize("name", RELAY_NAMES)
    def test_relay_energise_pin_high(self, io_expanders, name):
        """Verify TCA9555 output bit goes HIGH when relay is energised."""
        addr, port, bit = CFG.RELAY_PINS[name]
        exp = _relay_expander(io_expanders, name)
        if not exp.is_present():
            pytest.skip(f"TCA9555 @ 0x{addr:02X} not responding")

        exp.set_all_outputs()
        _energise(io_expanders, name, True)
        output = exp.read_all()[f"output_port{port}"]
        assert output & (1 << bit), (
            f"{name}: energised but TCA9555 port{port} bit{bit} is LOW. "
            "Check RELAY_PINS mapping in hw_config.py."
        )

    @pytest.mark.parametrize("name", RELAY_NAMES)
    def test_relay_deenergise_pin_low(self, io_expanders, name):
        """Verify TCA9555 output bit goes LOW when relay is de-energised."""
        addr, port, bit = CFG.RELAY_PINS[name]
        exp = _relay_expander(io_expanders, name)
        if not exp.is_present():
            pytest.skip(f"TCA9555 @ 0x{addr:02X} not responding")

        exp.set_all_outputs()
        _energise(io_expanders, name, True)
        _energise(io_expanders, name, False)
        output = exp.read_all()[f"output_port{port}"]
        assert not (output & (1 << bit)), (
            f"{name}: de-energised but TCA9555 port{port} bit{bit} is HIGH."
        )

    def test_only_one_relay_active_at_a_time(self, io_expanders):
        """Energise each relay in sequence; others must remain off."""
        # First check all expanders present
        for name in RELAY_NAMES:
            addr, _, _ = CFG.RELAY_PINS[name]
            exp = _relay_expander(io_expanders, name)
            if not exp.is_present():
                pytest.skip(f"TCA9555 @ 0x{addr:02X} not responding — "
                            "skipping isolation test")

        # Configure all relay pins as outputs
        for name in RELAY_NAMES:
            addr, port, bit = CFG.RELAY_PINS[name]
            exp = _relay_expander(io_expanders, name)
            exp.set_all_outputs()

        for active in RELAY_NAMES:
            _energise(io_expanders, active, True)
            for other in RELAY_NAMES:
                if other == active:
                    continue
                addr, port, bit = CFG.RELAY_PINS[other]
                exp = _relay_expander(io_expanders, other)
                output = exp.read_all()[f"output_port{port}"]
                assert not (output & (1 << bit)), (
                    f"Relay {active} energised, but {other} output pin "
                    f"(port{port} bit{bit}) is unexpectedly HIGH."
                )
            _energise(io_expanders, active, False)
