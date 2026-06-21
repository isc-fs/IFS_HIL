"""
Block A — Boot & bootloader compatibility (IFS08-CE-ECU#71).

A-004 (app boots -> 0x100) and A-007 (cold-boot via the real BL auto-jump ->
streams) are already proven by the 6/6 cold-boot soak (PR #70) and by Blocks
B/C here, so they're not re-implemented.

A-002 (BL discover), A-003 (flash + jump) and A-006 (BL round-trip 0x002) need
the can-flasher `flasher` fixture (ported from the AMS conftest) and, for A-003,
a flash cycle on the live carrier — deferred to a BL-tooling pass so the flash
risk is gated deliberately. Implemented here: A-001 (carrier power) + A-005
(fwinfo identity, static on the .bin).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


class TestA001CarrierPower:

    def test_a001_carrier_draws_current(self, mlc_powered, vcu_profile):
        """A-001: MLC4 draws boot current (~130 mA) after K4 closes — the carrier
        is alive (relay closed, fuse intact, STM32 running), not a short."""
        from broker.server import BrokerClient
        c = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                        "/run/hil-broker/broker.sock"))
        try:
            mA = c.call("ina.current", addr=mlc_powered["ina_addr"]) * 1000.0
        finally:
            c.close()
        floor = float(vcu_profile["mlc_boot_current_mA"])
        assert mA >= floor, f"MLC4 draws {mA:.0f} mA (< {floor:.0f} mA) — not alive"
        assert mA < 400, f"MLC4 draws {mA:.0f} mA — implausibly high (short?)"


@pytest.fixture(scope="session")
def ecu_firmware_bin():
    p = Path(os.environ.get("ECU_FIRMWARE_BIN",
                            os.path.expanduser("~/firmware-builds/ECU_fix.bin")))
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
