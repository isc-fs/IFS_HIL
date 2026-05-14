"""
Block E — Bootloader integration.

Pure-CAN tests. Don't require any GPIO routing beyond what we already have
(FDCAN1 on `can0`, FDCAN2 on `can2`). The boot-trigger frame is sent on
FDCAN2; wrong-bus tests send on FDCAN1.

Implemented:
  HIL-041  Boot-trigger round-trip
  HIL-042  Wrong-bus trigger ignored
  HIL-043  Wrong-payload trigger ignored (parametric, 6 sub-cases)
  HIL-044  Wrong-DLC trigger ignored
  HIL-046  BKP0R cleared by BL (one-shot)
  HIL-047  Flood of malformed + one valid

Deferred:
  HIL-045  Pre-reboot relay-open timing — needs µs-resolution on PD3/4/5
           which aren't routed off the MAIN_LITE connector.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_telemetry_silent(observe_acu, timeout_s: float = 2.0) -> bool:
    """Returns True if 0x4A0 telemetry stops within `timeout_s` (i.e. the
    app rebooted into BL). The check looks for a gap > 1s in the stream."""
    deadline = time.time() + timeout_s
    last_seen = time.time()
    last_count = observe_acu.count(M.ID_TELEM_STATUS)
    while time.time() < deadline:
        now_count = observe_acu.count(M.ID_TELEM_STATUS)
        if now_count > last_count:
            last_count = now_count
            last_seen = time.time()
        elif time.time() - last_seen > 1.0:
            return True
        time.sleep(0.05)
    return False


def _bl_alive(flasher) -> bool:
    return bool(flasher.discover())


# ---------------------------------------------------------------------------
# HIL-041: Boot-trigger round-trip
# ---------------------------------------------------------------------------

class TestBootTriggerRoundTrip:
    def test_trigger_drops_app_to_bl(self, observe_acu, flasher,
                                     ams_profile, bms_emulator):
        # Pre-condition: app is running (telemetry observable)
        time.sleep(0.6)
        assert observe_acu.last(M.ID_TELEM_STATUS) is not None, (
            "Pre-condition not met: AMS app must be running. Run Block A first."
        )

        # Fire the trigger on FDCAN2
        observe_acu.clear()
        flasher.send_boot_trigger()

        # Telemetry should stop within ~15 ms (reboot) — but the bench
        # observation has 50 ms poll granularity, so allow 1 s.
        assert _wait_telemetry_silent(observe_acu, timeout_s=2.0), (
            "Telemetry didn't stop after boot trigger — reboot didn't fire."
        )

        # And the BL should be alive on can2 again
        time.sleep(0.5)
        assert _bl_alive(flasher), (
            "BL not reachable after boot trigger. Either the trigger didn't "
            "make it to the firmware (check FDCAN2 wiring) or the app didn't "
            "call request_reboot()."
        )


# ---------------------------------------------------------------------------
# HIL-042: Wrong-bus trigger ignored
# ---------------------------------------------------------------------------

class TestWrongBusTriggerIgnored:
    def test_trigger_on_acu_bus_ignored(self, acu, observe_acu, current_state):
        time.sleep(0.6)
        assert current_state() is not None, "Need a running app for HIL-042"

        # Send the exact trigger payload but on FDCAN1 (kernel can0)
        acu.send_raw(0x002, M.BOOT_TRIGGER_PAYLOAD, is_extended_id=False)
        time.sleep(0.5)

        # App must still be alive
        frame = observe_acu.last(M.ID_TELEM_STATUS)
        assert frame is not None, (
            "Telemetry stopped after trigger on FDCAN1 — wrong-bus filter "
            "is leaky."
        )


# ---------------------------------------------------------------------------
# HIL-043: Wrong-payload trigger ignored (parametric)
# ---------------------------------------------------------------------------

_BAD_PAYLOADS = [
    bytes([0x00, 0x07, 0xAD, 0x11]),   # byte 0 zeroed
    bytes([0xB0, 0x00, 0xAD, 0x11]),
    bytes([0xB0, 0x07, 0x00, 0x11]),
    bytes([0xB0, 0x07, 0xAD, 0x00]),
    bytes([0xFF, 0xFF, 0xFF, 0xFF]),
    bytes([0xB0, 0x07, 0xAD, 0x12]),   # byte 3 off by one
]


@pytest.mark.parametrize("bad_payload", _BAD_PAYLOADS,
                         ids=[p.hex().upper() for p in _BAD_PAYLOADS])
class TestWrongPayloadTriggerIgnored:
    def test_each_bad_payload_ignored(self, ams_profile, observe_acu,
                                      bad_payload):
        time.sleep(0.5)
        assert observe_acu.last(M.ID_TELEM_STATUS), "Need a running app"

        bus = ams_profile["bus_bms_bl"]
        msg = f"002#{bad_payload.hex().upper()}"
        subprocess.run(["cansend", bus, msg], check=True, capture_output=True)
        time.sleep(0.3)

        # Telemetry must still be live (app didn't reboot)
        assert observe_acu.last(M.ID_TELEM_STATUS).timestamp > time.time() - 0.7, (
            f"Bad payload {bad_payload.hex().upper()} caused a reboot."
        )


# ---------------------------------------------------------------------------
# HIL-044: Wrong-DLC trigger ignored
# ---------------------------------------------------------------------------

class TestWrongDlcTriggerIgnored:
    def test_dlc_3_ignored(self, ams_profile, observe_acu):
        bus = ams_profile["bus_bms_bl"]
        subprocess.run(["cansend", bus, "002#B007AD"], check=True, capture_output=True)
        time.sleep(0.3)
        assert observe_acu.last(M.ID_TELEM_STATUS).timestamp > time.time() - 0.7

    def test_dlc_5_ignored(self, ams_profile, observe_acu):
        bus = ams_profile["bus_bms_bl"]
        subprocess.run(["cansend", bus, "002#B007AD1100"], check=True, capture_output=True)
        time.sleep(0.3)
        assert observe_acu.last(M.ID_TELEM_STATUS).timestamp > time.time() - 0.7

    def test_dlc_8_ignored(self, ams_profile, observe_acu):
        bus = ams_profile["bus_bms_bl"]
        subprocess.run(["cansend", bus, "002#B007AD11FFFFFFFF"], check=True, capture_output=True)
        time.sleep(0.3)
        assert observe_acu.last(M.ID_TELEM_STATUS).timestamp > time.time() - 0.7


# ---------------------------------------------------------------------------
# HIL-046: BKP0R cleared by BL (one-shot)
# ---------------------------------------------------------------------------

class TestBkp0rOneShot:
    """After a boot-trigger reboot, the BL clears BKP0R as it consumes the
    magic. A subsequent reset (e.g. via a second BL `reset` command, or a
    power cycle) should bring the app back, not the BL."""

    def test_second_reset_boots_app(self, flasher, observe_acu, ams_profile,
                                    ams_firmware_bin):
        # Get into BL via the trigger path
        flasher.send_boot_trigger()
        time.sleep(0.8)
        assert _bl_alive(flasher), "BL not reachable after first trigger"

        # We need an app present to reboot into. If the chip is blank in
        # the app slot the BL will just stay in BL — skip if so.
        if not ams_firmware_bin.is_file():
            pytest.skip("No AMS_FIRMWARE_BIN — flash the app first")

        # Trigger a plain reset via cansend of any "0x01" frame is tooling-
        # dependent; the BL's `--reset` semantic depends on can-flasher
        # version. Easier: send a `send-raw 0x001 03 06 01` which is the
        # "app to BL" — but we want BL to jump to app. Use power cycle:
        from broker.server import BrokerClient
        import os
        c = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                        "/run/hil-broker/broker.sock"))
        slot = int(ams_profile["mlc_slot"])
        relay_bit = slot - 1
        c.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
        time.sleep(0.5)
        c.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=True)
        time.sleep(float(ams_profile["mlc_boot_settle_s"]))
        c.close()

        # After the second boot, BL should NOT be the responder (app is)
        nodes = flasher.discover()
        assert nodes == [], (
            "BL still responding after power-cycle following a boot-trigger. "
            "Either BKP0R wasn't cleared (BL one-shot violated), or the app "
            "isn't installed."
        )


# ---------------------------------------------------------------------------
# HIL-047: Flood of malformed + one valid
# ---------------------------------------------------------------------------

class TestTriggerFloodResilience:
    def test_100_bad_plus_1_valid(self, ams_profile, observe_acu, flasher):
        time.sleep(0.5)
        assert observe_acu.last(M.ID_TELEM_STATUS), "Need a running app"

        bus = ams_profile["bus_bms_bl"]
        # 100 frames of B007AD12 (byte 3 off by one) over ~1 s
        for _ in range(100):
            subprocess.run(["cansend", bus, "002#B007AD12"],
                           check=True, capture_output=True)
            time.sleep(0.01)

        # The 100 bad frames must NOT have caused a reboot
        assert observe_acu.last(M.ID_TELEM_STATUS).timestamp > time.time() - 0.5, (
            "Reboot fired during the flood of malformed triggers."
        )

        # Now fire the valid one
        observe_acu.clear()
        flasher.send_boot_trigger()
        assert _wait_telemetry_silent(observe_acu, timeout_s=2.0), (
            "Valid trigger after a flood didn't take effect — flood may have "
            "filled an internal queue."
        )
        time.sleep(0.5)
        assert _bl_alive(flasher), "BL not reachable after the post-flood trigger"
