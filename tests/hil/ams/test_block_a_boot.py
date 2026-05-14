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


# HIL-004 ------------------------------------------------------------------

class TestAppReachesStart:
    """HIL-004: post-flash reset hands off cleanly to the app, the FSM comes
    up in Start, and the 500 ms telemetry stream is alive.

    The firmware drops UART telemetry in favour of three CAN frames on
    FDCAN1 (`0x4A0` AMS status, `0x4A1` pack, `0x4A2` temps + diagnostics).
    HIL-004 is therefore observable purely from the bench's CAN sniff."""

    def test_app_boots_and_reaches_start(self, ams_firmware_bin, flasher,
                                         bms_emulator, observe_acu,
                                         ams_profile):
        from tools.firmware_test.ams import can_map as M
        import time

        # Get into BL if not already (HIL-003 path may have left an app)
        if not flasher.discover():
            flasher.send_boot_trigger()
            time.sleep(1.5)

        # Flash + jump
        observe_acu.clear()
        flasher.flash(ams_firmware_bin,
                      address=int(ams_profile["app_flash_address"]),
                      verify=True, jump=True,
                      timeout_s=float(ams_profile["bl_flash_timeout_s"]))

        # Wait for first 0x4A0 — gives the app ~2 cadence intervals to settle
        deadline = time.time() + 2 * (int(ams_profile["tx_telemetry_period_ms"]) / 1000.0) + 1.0
        first = None
        while time.time() < deadline:
            first = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if first is not None:
                break
            time.sleep(0.05)

        assert first is not None, (
            f"No 0x4A0 telemetry within {2 * ams_profile['tx_telemetry_period_ms']} ms "
            "of jump. App didn't reach the telemetry task, or FDCAN1 frame format "
            "is still FD (re-check the classic-CAN switch landed)."
        )

        snap = M.decode_telem_status(first.data)
        assert snap["state"] == M.FsmState.START, (
            f"First state was {snap['state_name']} (expected Start). "
            "Either the app booted into a fault, or BMS / SDC defaults trip ERROR "
            "before the first telemetry frame."
        )
