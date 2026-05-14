"""
Block A — Boot & bring-up.

Adapted subset of `IFS08-CE-AMS/docs/HIL_TESTS.md` HIL-001..009 for the
IFS08_HIL bench with a bare MAIN_LITE carrier in MLC1. Tests that need
SWD / GDB / GPIO-pin readback are deferred until the firmware is re-pinned
to MAIN_LITE GPIOs (or a Nucleo-with-breakouts rig is added).

Currently implemented:

  HIL-002  Bootloader is alive and identifies itself
  HIL-003  AMS firmware flashes via the bootloader (requires a built .bin)

To run:

    pytest tests/hil/ams/test_block_a_boot.py -v
    AMS_FIRMWARE_BIN=/path/to/AMS.bin pytest tests/hil/ams/test_block_a_boot.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# HIL-002 ------------------------------------------------------------------

class TestBlBringUp:
    """HIL-002: bootloader responds to DISCOVER on FDCAN2."""

    def test_bl_discover(self, flasher):
        nodes = flasher.discover()
        assert nodes, (
            "No bootloaders replied on the BL bus. Check: "
            "(1) the carrier in MLC1 has the BL flashed, "
            "(2) ams_profile.bus_bms_bl matches the kernel netdev wired to "
            "the carrier's CAN2 transceiver."
        )
        assert len(nodes) == 1, (
            f"Expected exactly one node on the BL bus, got {len(nodes)}. "
            "Either multiple carriers are powered, or distinct node IDs need "
            "provisioning (`can-flasher … config --set node-id 0xN`)."
        )
        n = nodes[0]
        assert n.node_id == flasher.node_id, (
            f"BL responded with node 0x{n.node_id:02X}, expected "
            f"0x{flasher.node_id:02X} (from ams_profile.bl_node_id)."
        )


# HIL-003 ------------------------------------------------------------------

@pytest.fixture(scope="session")
def ams_firmware_bin(ams_profile) -> Path:
    """Path to the AMS app .bin. Default location is `/tmp/AMS.bin`; override
    with `AMS_FIRMWARE_BIN`. Skips if the file isn't present so test runs in
    CI without a built firmware stay clean."""
    p = Path(os.environ.get("AMS_FIRMWARE_BIN", "/tmp/AMS.bin"))
    if not p.is_file():
        pytest.skip(f"AMS firmware not found at {p} (set AMS_FIRMWARE_BIN)")
    return p


class TestAppFlash:
    """HIL-003: AMS .bin installs in-system via the BL flash protocol."""

    def test_app_flashes_via_bl(self, flasher, ams_firmware_bin, ams_profile):
        # Sanity: BL is the one currently listening (no app already running)
        nodes_before = flasher.discover()
        if not nodes_before:
            # If an app is already running, try to drop it back to BL first
            flasher.send_boot_trigger()
            import time; time.sleep(1.5)
            nodes_before = flasher.discover()
        assert nodes_before, "BL not reachable even after boot-trigger; cannot flash"

        # Flash + verify + jump
        r = flasher.flash(
            ams_firmware_bin,
            address=int(ams_profile["app_flash_address"]),
            verify=True,
            jump=True,
            timeout_s=float(ams_profile["bl_flash_timeout_s"]),
        )
        assert "Done" in r.stdout, f"flash output missing Done marker:\n{r.stdout}"

        # After --jump, BL is gone (app is running). discover should be empty.
        import time; time.sleep(0.5)
        nodes_after = flasher.discover()
        assert nodes_after == [], (
            "BL still answering after flash --jump. The app didn't boot, "
            "or the BL didn't relinquish control."
        )


# HIL-004 placeholder ------------------------------------------------------
#
# Confirming "app reaches Start" requires either UART telemetry (USART2 not
# routed off the MAIN_LITE connector) or observing AMS TX cadence on FDCAN1.
# Implement when test_block_d lands the cadence helpers; tracked here as a
# skip so the test ID is visible in `pytest --collect-only`.

@pytest.mark.skip(reason="HIL-004 needs UART or FDCAN1-TX observability — "
                  "rework once Block D's cadence helpers are in place.")
def test_hil_004_app_reaches_start():
    pass
