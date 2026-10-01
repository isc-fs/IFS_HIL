"""
Block A — Boot & bootloader compatibility (IFS08-CE-ECU#71).

A-004 (app boots -> 0x100) and A-007 (cold-boot via the real BL auto-jump ->
streams) are already proven by the 6/6 cold-boot soak (PR #70) and Blocks B/C.

A-001 carrier power, A-005 fwinfo identity, A-002 BL discover, A-003 flash+jump,
A-006 BL round-trip. The BL runs at SP 87.5% while the app's FDCAN2 is 68.75%,
so the BL ops flip can2 to 0.875 and restore 0.688 after. Each BL test power-
cycles to a fresh app boot first, so 0x002 always hits a fully-running app.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from tools.firmware_test.vcu import can_map as M

_BL_SP = 0.875                      # the ECU bootloader's FDCAN sample point
_BL_TRIGGER_ID = 0x002
_BL_TRIGGER_PAYLOAD = "B007AD12"   # ECU bootloader.hpp BlBootTriggerPayload


def _broker():
    from broker.server import BrokerClient
    return BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                       "/run/hil-broker/broker.sock"))


def _set_can_sp(channel: str, sp: float, bitrate: int = 500_000) -> None:
    """Flip a SocketCAN channel's sample point (BL 0.875 vs app 0.688), keeping
    txqueuelen=1000 (invariant #5), then settle. Skips if it can't."""
    try:
        subprocess.run(["sudo", "ip", "link", "set", channel, "down"],
                       check=False, timeout=5)
        subprocess.run(["sudo", "ip", "link", "set", channel, "type", "can",
                        "bitrate", str(bitrate), "sample-point", f"{sp:.3f}",
                        "restart-ms", "200"], check=True, timeout=5)
        subprocess.run(["sudo", "ip", "link", "set", channel, "txqueuelen", "1000"],
                       check=False, timeout=5)
        subprocess.run(["sudo", "ip", "link", "set", channel, "up"],
                       check=True, timeout=5)
        time.sleep(0.4)
    except Exception as e:
        pytest.skip(f"could not set SP {sp} on {channel}: {e}")


def _power_cycle_to_app(vcu_profile, mlc_powered, timeout_s: float = 8.0) -> None:
    """Relay power-cycle MLC4 to a fresh app boot and wait for 0x100, so the
    subsequent 0x002 hits a fully-running app. Bus must be at the app SP."""
    from tools.firmware_test.can_observer import CanObserver
    c = _broker()
    rb = mlc_powered["relay_bit"]
    try:
        c.call("tca.write_pin", addr=0x20, port=0, pin=rb, value=False)
        time.sleep(2.0)
        c.call("tca.write_pin", addr=0x20, port=0, pin=rb, value=True)
    finally:
        c.close()
    with CanObserver(channel=vcu_profile["bus_acu"]) as obs:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if obs.last(M.ID_HEARTBEAT, extended=False) is not None:
                return
            time.sleep(0.05)


def _reboot_to_bl(vcu_profile) -> None:
    """Send 0x002/0xB007AD12 on the ACU bus at the app SP -> the running app
    reboots into the bootloader (BKP0R magic) and stays. Then settle."""
    from tools.firmware_test.acu_stim import AcuStim
    stim = AcuStim(channel=vcu_profile["bus_acu"])
    stim.start()
    try:
        for _ in range(6):
            stim.send_raw(_BL_TRIGGER_ID, bytes.fromhex(_BL_TRIGGER_PAYLOAD),
                          is_extended_id=False)
            time.sleep(0.1)
    finally:
        stim.stop()
    time.sleep(2.5)


def _to_bl_and_discover(flasher, vcu_profile, mlc_powered, tries: int = 5):
    """Fresh app boot -> 0x002 reboot-to-BL -> flip can2 to the BL SP -> discover
    (with retries). Caller restores the app SP in a finally."""
    _power_cycle_to_app(vcu_profile, mlc_powered)
    _reboot_to_bl(vcu_profile)
    _set_can_sp(vcu_profile["bus_flash"], _BL_SP)
    nodes = []
    for _ in range(tries):
        nodes = flasher.discover()
        if nodes:
            break
        time.sleep(0.8)
    return nodes


class TestA001CarrierPower:

    def test_a001_carrier_draws_current(self, mlc_powered, vcu_profile):
        """A-001: MLC4 draws boot current (~130 mA) after K4 closes — the carrier
        is alive (relay closed, fuse intact, STM32 running), not a short."""
        c = _broker()
        try:
            mA = c.call("ina.current", addr=mlc_powered["ina_addr"]) * 1000.0
        finally:
            c.close()
        floor = float(vcu_profile["mlc_boot_current_mA"])
        assert mA >= floor, f"MLC4 draws {mA:.0f} mA (< {floor:.0f} mA) — not alive"
        assert mA < 400, f"MLC4 draws {mA:.0f} mA — implausibly high (short?)"


@pytest.fixture(scope="session")
def ecu_firmware_bin():
    """Path to the ECU app .bin that A-003 reflashes.

    A-003 REFLASHES the carrier from this path, so it must be the image under
    test. When the variable was unset it fell back to a hardcoded local file --
    and on bench-01 the ECU fallback (~/firmware-builds/ECU_fix.bin, a
    2026-06-21 diagnostic build) EXISTS. A dispatched run therefore flashed the
    PR firmware, then A-003 quietly flashed the June binary over it at 7 %, and
    the remaining 56 cases reported on that instead. A-003 passed while doing
    it: it only asserts the flash succeeded and 0x100 came back.

    Under CI the fallback is now a hard failure -- a dispatched run must be told
    which image it is testing. Local runs keep the convenience path.
    """
    env = os.environ.get("ECU_FIRMWARE_BIN")
    if not env and os.environ.get("GITHUB_ACTIONS"):
        pytest.fail(
            "ECU_FIRMWARE_BIN is unset in CI. A dispatched run must point the "
            "reflash fixtures at the image under test, or A-003 flashes a stale "
            "local binary over it and every later case reports on that. "
            "hil-test.yml exports this right after flashing.")
    p = Path(env or os.path.expanduser("~/firmware-builds/ECU_fix.bin"))
    if not p.is_file():
        pytest.skip(f"ECU firmware .bin not found at {p} (set ECU_FIRMWARE_BIN)")
    return p


class TestA005FirmwareInfo:

    def test_a005_fwinfo_magic_and_product(self, ecu_firmware_bin, vcu_profile):
        """A-005: the fwinfo record at .bin offset 0x400 (= 0x08020400) carries
        the magic 0xF14F1B00 and the product string 'IFS08-CE-ECU'."""
        data = Path(ecu_firmware_bin).read_bytes()
        off = int(vcu_profile["firmware_info_address"]) - \
            int(vcu_profile["app_flash_address"])
        assert len(data) > off + 0x80, \
            f".bin too small ({len(data)} B) for a fwinfo record at 0x{off:X}"
        rec = data[off:off + 0x80]
        magic_le = int.from_bytes(rec[0:4], "little")
        magic_be = int.from_bytes(rec[0:4], "big")
        assert 0xF14F1B00 in (magic_le, magic_be), \
            f"fwinfo magic {rec[0:4].hex()} != 0xF14F1B00 (record @0x{off:X})"
        assert b"IFS08-CE-ECU" in rec, \
            "product string 'IFS08-CE-ECU' not found in the fwinfo record"


class TestA002BootloaderDiscover:

    def test_a002_bl_discover(self, mlc_powered, flasher, vcu_profile):
        """A-002: with the app rebooted to the BL, the bootloader is discoverable
        on the shared FDCAN2 bus and replies node-id 0x01, product IFS08-CE-ECU."""
        try:
            nodes = _to_bl_and_discover(flasher, vcu_profile, mlc_powered)
            assert nodes, "no bootloader replied on FDCAN2 after the 0x002 reboot"
            n = nodes[0]
            assert n.node_id == int(vcu_profile["bl_node_id"]), \
                f"BL node 0x{n.node_id:02X}, expected " \
                f"0x{int(vcu_profile['bl_node_id']):02X}"
            assert "IFS08-CE-ECU" in n.product, \
                f"BL product {n.product!r} != IFS08-CE-ECU"
        finally:
            _set_can_sp(vcu_profile["bus_flash"],
                        float(vcu_profile["bus_acu_sample_point"]))


class TestA006BootloaderRoundTrip:

    def test_a006_bl_round_trip(self, mlc_powered, flasher, vcu_profile):
        """A-006: 0x002=0xB007AD12 reboots the running app to the BL and the CAN
        path home survives — the BL is reachable on the same bus afterwards."""
        try:
            nodes = _to_bl_and_discover(flasher, vcu_profile, mlc_powered)
            assert nodes and nodes[0].node_id == int(vcu_profile["bl_node_id"]), \
                "0x002 didn't land a discoverable BL — the path home is broken"
        finally:
            _set_can_sp(vcu_profile["bus_flash"],
                        float(vcu_profile["bus_acu_sample_point"]))


class TestA003FlashAndJump:

    def test_a003_flash_and_jump(self, mlc_powered, flasher, ecu_firmware_bin,
                                 vcu_profile):
        """A-003: from the BL, flash the app image @0x08020000 with verify +
        jump; the app comes back up streaming 0x100."""
        from tools.firmware_test.can_observer import CanObserver
        bus = vcu_profile["bus_flash"]
        app_sp = float(vcu_profile["bus_acu_sample_point"])
        try:
            assert _to_bl_and_discover(flasher, vcu_profile, mlc_powered), \
                "BL not reachable; cannot flash"
            r = flasher.flash(str(ecu_firmware_bin),
                              address=int(vcu_profile["app_flash_address"]),
                              verify=True, jump=True, extra_args=["--yes"],
                              timeout_s=float(vcu_profile["bl_flash_timeout_s"]))
            out = (r.stdout or "") + (r.stderr or "")
            assert "Flashed" in out or "already matches" in out.lower(), \
                f"flash output missing a success marker:\n{out}"
        finally:
            _set_can_sp(bus, app_sp)
        # --jump landed -> the app should boot and stream 0x100 at the app SP
        with CanObserver(channel=bus) as obs:
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if obs.last(M.ID_HEARTBEAT, extended=False) is not None:
                    return
                time.sleep(0.05)
        raise AssertionError("no 0x100 within 8 s of flash+jump — app didn't boot")
