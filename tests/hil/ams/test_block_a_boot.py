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

    def test_bl_discover(self, flasher, ams_profile):
        nodes = flasher.discover()
        if not nodes:
            # An app may already be running from a previous session's
            # flash+jump. Drop it back to BL via the ACU-bus trigger
            # (FDCAN1 per the AMS v1.2.0-ltc6811 refactor #73) and retry.
            import time
            flasher.send_boot_trigger(channel=ams_profile["bus_acu"])
            time.sleep(0.5)
            nodes = flasher.discover()
        assert nodes, (
            "No bootloaders replied on the BL bus. Check: "
            "(1) the carrier in MLC1 has the BL flashed, "
            "(2) ams_profile.bus_bms_bl matches the kernel netdev wired to "
            "the carrier's CAN2 transceiver, "
            "(3) any running app responds to the boot-trigger frame on bus_acu."
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
            # App was already running — drop it back to BL via FDCAN1
            # (per AMS v1.2.0-ltc6811 refactor #73). Retry discover after.
            import time
            flasher.send_boot_trigger(channel=ams_profile["bus_acu"])
            time.sleep(0.5)
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
        # `can-flasher` exits non-zero on protocol failure (the `_run`
        # wrapper would have raised), so reaching this point already
        # proves the flash succeeded. Belt-and-braces: check for the
        # structured success markers, which live on stdout *or* stderr
        # depending on the can-flasher version (1.3.x split progress to
        # stderr while keeping the summary on stdout).
        combined = (r.stdout or "") + "\n" + (r.stderr or "")
        assert "Done" in combined, (
            f"flash output missing 'Done' marker:\n--- stdout ---\n{r.stdout}\n"
            f"--- stderr ---\n{r.stderr}"
        )
        assert "jumped to app" in combined, (
            f"flash output missing jump confirmation:\n--- stdout ---\n{r.stdout}\n"
            f"--- stderr ---\n{r.stderr}"
        )

        # We deliberately do NOT assert "BL is silent after jump" here:
        # whether the app stays alive depends on bench stim (SDC, BMS,
        # ACU heartbeat) that this test doesn't set up. HIL-003 is about
        # the BL flash protocol, not app boot — that's HIL-004's job.
        # We only check the flasher itself reports a successful jump.


# HIL-004 ------------------------------------------------------------------

class TestAppReachesStart:
    """HIL-004: post-flash reset hands off cleanly to the app, the FSM comes
    up in Start, and the 500 ms telemetry stream is alive.

    The firmware drops UART telemetry in favour of three CAN frames on
    FDCAN1 (`0x4A0` AMS status, `0x4A1` pack, `0x4A2` temps + diagnostics).
    HIL-004 is therefore observable purely from the bench's CAN sniff."""

    def test_app_boots_and_reaches_start(self, ams_firmware_bin, flasher,
                                         bms_emulator, observe_acu,
                                         acu_heartbeat,
                                         ams_profile):
        from tools.firmware_test.ams import can_map as M
        import time

        # Strategy: power-cycle MLC1, let the BL auto-jump to the
        # already-installed app, then observe the FIRST `0x4A0` frame.
        # We deliberately don't re-flash here — HIL-003 already proved
        # the flash protocol works, and the AMS app's boot-trigger path
        # (CAN frame `0x002`#`B007AD11` on FDCAN1) doesn't reliably
        # drop a running app back to BL on this firmware build
        # (tracked separately). Cleanest reset is a hardware power-cycle.
        #
        # `acu_heartbeat` + `bms_emulator` must already be live before
        # the app boots so SafetyTask's predicates have fresh data once
        # the boot grace expires.

        from broker.server import BrokerClient
        import os
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        slot_pin = int(ams_profile["mlc_slot"]) - 1
        try:
            # K1 open → drain → K1 close. Settle long enough for BL +
            # auto-jump; the app will start emitting `0x4A0` within
            # `tx_telemetry_period_ms` of jump.
            client.call("tca.write_pin", addr=0x20, port=0, pin=slot_pin, value=False)
            time.sleep(2.0)
            observe_acu.clear()
            client.call("tca.write_pin", addr=0x20, port=0, pin=slot_pin, value=True)
        finally:
            client.close()

        # Wait for the first `0x4A0` frame post-app-boot. BL boot +
        # auto-jump is < 3 s on this carrier; budget 5 s.
        deadline = time.time() + 5.0
        first = None
        while time.time() < deadline:
            first = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if first is not None:
                break
            time.sleep(0.05)

        assert first is not None, (
            "No 0x4A0 telemetry within 5 s of power-cycle. The app didn't "
            "reach the telemetry task — either the BL didn't auto-jump, "
            "or FDCAN1 isn't transmitting. Check INA1 current and "
            "`candump can0` to localise."
        )

        snap = M.decode_telem_status(first.data)
        assert snap["state"] == M.FsmState.START, (
            f"First state was {snap['state_name']} (expected Start). "
            "Either the app booted into a fault, or BMS / SDC defaults trip ERROR "
            "before the first telemetry frame."
        )
