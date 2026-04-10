"""
CAN CONTROLLER TESTS  —  3× MCP2515
=====================================
Tests (no external CAN bus required — uses MCP2515 internal loopback mode):

  1. Each MCP2515 resets cleanly and enters config mode.
  2. Bitrate registers are written and confirmed in config mode.
  3. Loopback self-test: transmit a frame, verify the chip receives it.
  4. Error counters are zero after a successful loopback.
  5. INT pins are readable as GPIO inputs (not stuck high or stuck low
     with no pending interrupts).

For a bus-connected test (two chips communicating over physical CAN H/L),
see test_can_bus.py (not included — requires loopback plug or second node).
"""

import time
import pytest
import RPi.GPIO as GPIO
from tools import hw_config as CFG
from tools.mcp2515 import MCP2515

CAN_NAMES = ["CAN1 (U17)", "CAN2 (U19)", "CAN3 (U21)"]
INT_PINS   = [CFG.INT_CAN1, CFG.INT_CAN2, CFG.INT_CAN3]

_MODE_CONFIG    = 0x80
_MODE_LOOPBACK  = 0x40
_MODE_NORMAL    = 0x00

# Test CAN frame
_TEST_CAN_ID   = 0x1AB
_TEST_DATA     = bytes([0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF])


class TestMCP2515Reset:
    """Verify each chip resets and reports CONFIG mode."""

    @pytest.mark.parametrize("idx", range(3), ids=CAN_NAMES)
    def test_reset_enters_config_mode(self, can_controllers, idx):
        ctrl = can_controllers[idx]
        ctrl.reset()
        mode = ctrl.get_mode()
        assert mode == _MODE_CONFIG, (
            f"{CAN_NAMES[idx]}: after reset, mode = 0x{mode:02X}, "
            f"expected CONFIG (0x{_MODE_CONFIG:02X}). "
            "Check SPI CS pin and SPI bus wiring."
        )


class TestMCP2515Init:
    """Initialise at 500 kbit/s and confirm register values."""

    @pytest.mark.parametrize("idx", range(3), ids=CAN_NAMES)
    def test_init_500kbps(self, can_controllers, idx):
        ctrl = can_controllers[idx]
        ok = ctrl.init(bitrate=CFG.CAN_BITRATE)
        assert ok, (
            f"{CAN_NAMES[idx]}: init failed — chip did not enter config mode "
            f"after reset. CS pin = GPIO{[CFG.CS_CAN1, CFG.CS_CAN2, CFG.CS_CAN3][idx]}."
        )


class TestMCP2515Loopback:
    """Full loopback self-test: TX → internal → RX."""

    @pytest.mark.parametrize("idx", range(3), ids=CAN_NAMES)
    def test_loopback_short_frame(self, can_controllers, idx):
        """Transmit a 4-byte frame and verify reception."""
        ctrl = can_controllers[idx]
        ctrl.init(bitrate=CFG.CAN_BITRATE)
        ok = ctrl.loopback_test(can_id=0x123, data=b'\xDE\xAD\xBE\xEF')
        assert ok, (
            f"{CAN_NAMES[idx]}: loopback FAILED with 4-byte frame. "
            "Possible causes: incorrect bitrate config, SPI bus conflict, "
            "or wrong CS assignment in hw_config.py."
        )

    @pytest.mark.parametrize("idx", range(3), ids=CAN_NAMES)
    def test_loopback_full_frame(self, can_controllers, idx):
        """Transmit a full 8-byte DLC frame and verify reception."""
        ctrl = can_controllers[idx]
        ctrl.init(bitrate=CFG.CAN_BITRATE)
        ok = ctrl.loopback_test(can_id=_TEST_CAN_ID, data=_TEST_DATA)
        assert ok, (
            f"{CAN_NAMES[idx]}: loopback FAILED with 8-byte frame "
            f"(ID=0x{_TEST_CAN_ID:03X})."
        )

    @pytest.mark.parametrize("idx", range(3), ids=CAN_NAMES)
    def test_error_counters_zero(self, can_controllers, idx):
        """After successful loopback, TEC and REC should be 0."""
        ctrl = can_controllers[idx]
        ctrl.init(bitrate=CFG.CAN_BITRATE)
        ctrl.loopback_test(can_id=0x7FF, data=b'\x00')
        tec, rec = ctrl.read_error_counters()
        assert tec == 0, f"{CAN_NAMES[idx]}: TEC = {tec} (expected 0)"
        assert rec == 0, f"{CAN_NAMES[idx]}: REC = {rec} (expected 0)"


class TestMCP2515IntPins:
    """Verify INT GPIO pins are readable and are HIGH (no pending interrupts)."""

    @pytest.mark.parametrize("idx", range(3), ids=CAN_NAMES)
    def test_int_pin_idle_high(self, can_controllers, idx):
        """
        After init with no pending frames, INT# should be HIGH.
        The MCP2515 INT pin is active-low; it de-asserts (high) when no
        interrupt is pending.
        """
        int_pin = INT_PINS[idx]
        ctrl = can_controllers[idx]
        ctrl.init(bitrate=CFG.CAN_BITRATE)
        # Small delay for any residual interrupt to clear
        time.sleep(0.01)
        level = GPIO.input(int_pin)
        assert level == GPIO.HIGH, (
            f"{CAN_NAMES[idx]}: INT pin (GPIO{int_pin}) is LOW with no pending "
            "interrupt. Check INT pin assignment in hw_config.py."
        )
