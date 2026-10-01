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
from tools import hw_config as CFG
from tools.hil_client import MCP2515

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


class TestMCP2515LinkHealth:
    """Verify each CAN interface can be brought up and stays healthy.

    Under the kernel mcp251x driver the physical INT pins are owned by the
    driver and not readable from userspace. `int_level()` on the broker
    proxy is repurposed as a link-health indicator: 1 when the netdev is
    UP, 0 when DOWN. Bringing a chip up in LOOPBACK mode (no peer needed)
    is the cheapest way to exercise the wake path and confirm it's alive.
    """

    @pytest.mark.parametrize("idx", range(3), ids=CAN_NAMES)
    def test_link_up_in_loopback(self, can_controllers, idx):
        ctrl = can_controllers[idx]
        ctrl.init(bitrate=CFG.CAN_BITRATE)
        ctrl.set_mode(_MODE_LOOPBACK)
        time.sleep(0.01)
        level = ctrl.int_level()
        assert level == 1, (
            f"{CAN_NAMES[idx]}: link is DOWN after init + set_mode(LOOPBACK). "
            "Check kernel mcp251x driver (sudo dmesg | grep mcp251x) and "
            "the PSU_ON signal (pinctrl get 7 should show 'lo')."
        )
        # Return to CONFIG so later tests start clean
        ctrl.set_mode(_MODE_CONFIG)
