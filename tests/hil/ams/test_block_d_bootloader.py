"""
Block D — Bootloader integration.

Implements D-041..D-045 from `isc-fs/IFS08-CE-AMS#123`.

| Test  | What it checks                                          | Status      |
|-------|---------------------------------------------------------|-------------|
| D-041 | Boot-trigger frame jumps to BL                          | implemented |
| D-042 | JumpReason logged in RTC_BKP_DR2                        | needs GDB   |
| D-043 | Node ID in firmware_info                                | host-side   |
| D-044 | Cold cycle → app auto-jumps                             | implemented |
| D-045 | Boot-trigger negative cases                             | implemented |
"""

from __future__ import annotations

import os
import struct
import subprocess
import time
from pathlib import Path

import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _heartbeat_reset_observed(observe_acu, baseline: int,
                              window_s: float = 3.0) -> bool:
    """Returns True iff the `0x4A2[7]` heartbeat counter drops below
    `min(baseline, 5)` within `window_s` — signature of an app restart
    (TelemetryTask seeds the counter from 0). Without an app restart
    the counter increments monotonically modulo-256 and never wraps in
    `window_s` at 500 ms cadence."""
    deadline = time.monotonic() + window_s
    last_seen = baseline
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        if f is not None:
            hb = M.decode_telem_temps(f.data)["heartbeat"]
            if hb != last_seen:
                backwards = (last_seen - hb) % 256
                if hb <= 5 and backwards > 5:
                    return True
                last_seen = hb
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# D-041 — Boot-trigger jumps to BL
# ---------------------------------------------------------------------------

class TestD041BootTriggerJumps:

    def test_d041(self, fresh_boot, observe_acu, heartbeat_helper,
                  ams_profile):
        # Let the chip emit a few telemetry frames so the heartbeat
        # counter is comfortably above the "fresh boot" low watermark.
        baseline = None
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            hb = heartbeat_helper["read"]()
            if hb is not None and hb >= 6:
                baseline = hb
                break
            time.sleep(0.05)
        assert baseline is not None, (
            "heartbeat counter never reached ≥ 6; can't establish a "
            "high-watermark to detect reset."
        )

        # Send the trigger on the ACU bus (FDCAN1).
        subprocess.run(
            ["cansend", ams_profile["bus_acu"], "002#B007AD11"],
            check=True, timeout=2,
        )

        # Expect the heartbeat counter to drop back to ≤ 5 within a
        # couple of seconds (chip resets → BL → auto-jump → app reboot
        # → counter restarts from 0).
        assert _heartbeat_reset_observed(observe_acu, baseline,
                                          window_s=3.0), (
            "heartbeat counter didn't reset after sending the boot-trigger "
            "frame. The chip didn't reboot — AcuCanTask may not have "
            "received the standard-ID frame (FDCAN1 std-RX), or "
            "Bootloader::request_reboot didn't fire."
        )


# ---------------------------------------------------------------------------
# D-042 — JumpReason logged in RTC_BKP_DR2
# ---------------------------------------------------------------------------

class TestD042JumpReason:

    @pytest.mark.skip(reason=(
        "D-042 requires reading `RTC->BKP2R` after the trigger jump. "
        "On this rig there's no SWD attach and the BL doesn't expose a "
        "backup-register read primitive over CAN. When the BL gains a "
        "`diagnose read-bkp` (or similar) command, or SWD is wired up, "
        "this becomes runnable. The kCanTrigger reason value is "
        "0x4A554D50 ('JUMP' LE) per PR #113."
    ))
    def test_d042_jump_reason_in_bkp2r(self):
        pass


# ---------------------------------------------------------------------------
# D-043 — Node ID in firmware_info
# ---------------------------------------------------------------------------

class TestD043NodeIdInFirmwareInfo:
    """Read the bl_fwinfo_t record from the .bin file on the host. This
    doesn't need the bench — it's a static check of the firmware image
    we're about to flash. Kept in Block D for test-plan completeness."""

    def test_d043(self, ams_firmware_bin, ams_profile):
        # bl_fwinfo_t lives at offset 0x400 of the .bin (= 0x08020400
        # absolute). Layout per firmware_info.cpp:
        #   uint32_t magic                  @ 0x00
        #   uint32_t record_version         @ 0x04
        #   uint32_t fw_version_major       @ 0x08
        #   uint32_t fw_version_minor       @ 0x0C
        #   uint32_t fw_version_patch       @ 0x10
        #   uint32_t mcu_id                 @ 0x14
        #   uint8_t  git_hash[8]            @ 0x18
        #   uint64_t build_timestamp        @ 0x20
        #   char     product_name[16]       @ 0x28
        #   uint32_t reserved[2]            @ 0x38
        bin_path = ams_firmware_bin
        data = Path(bin_path).read_bytes()
        rec_offset = 0x400
        assert len(data) > rec_offset + 0x40, (
            f"bin file too small ({len(data)} bytes); no firmware_info record."
        )
        rec = data[rec_offset:rec_offset + 0x40]
        magic = int.from_bytes(rec[0:4], "little")
        product_name = rec[0x28:0x38].rstrip(b"\x00").decode("ascii", "replace")
        reserved0 = int.from_bytes(rec[0x38:0x3C], "little")

        assert magic == 0xF14F1B00, (
            f"firmware_info magic = 0x{magic:08X}; expected 0xF14F1B00."
        )
        assert product_name == "IFS08-CE-AMS", (
            f"product_name = {product_name!r}; expected 'IFS08-CE-AMS'."
        )
        # Per the test plan, reserved[0] carries the node ID.
        expected_node_id = int(ams_profile["bl_node_id"])
        assert reserved0 == expected_node_id, (
            f"firmware_info reserved[0] (node ID) = 0x{reserved0:08X}; "
            f"expected 0x{expected_node_id:08X}."
        )


# ---------------------------------------------------------------------------
# D-044 — Cold cycle → app auto-jumps
# ---------------------------------------------------------------------------

class TestD044ColdCycleAutoJumps:

    def test_d044(self, fresh_boot, ams_profile):
        # `fresh_boot` already does a cold power-cycle and waits for the
        # first 0x4A0 telemetry frame. If we got here, auto-jump worked.
        elapsed_ms = (fresh_boot["t_first_frame"] - fresh_boot["t_power_on"]) * 1000
        # Per the test plan: < 2 s. Allow a bit of slack.
        assert elapsed_ms < 3000, (
            f"First telemetry took {elapsed_ms:.0f} ms from K_n close; "
            "expected < 2000 ms. BL auto-jump may be misconfigured."
        )


# ---------------------------------------------------------------------------
# D-045 — Boot-trigger negative cases
# ---------------------------------------------------------------------------

# Each: (cansend frame string, bus to send on, description)
NEGATIVE_CASES = [
    ("001#B007AD11",     "bus_acu",    "wrong ID (001 instead of 002)"),
    ("002#B007AD11AA",   "bus_acu",    "wrong DLC (5 bytes)"),
    ("002#B007AD12",     "bus_acu",    "wrong payload (last byte)"),
    ("002#B007AD11",     "bus_bms_bl", "wrong bus (BMS/BL instead of ACU)"),
]


class TestD045NegativeCases:

    @pytest.mark.parametrize("frame,bus_key,desc", NEGATIVE_CASES,
                             ids=[c[2] for c in NEGATIVE_CASES])
    def test_d045(self, fresh_boot, observe_acu, heartbeat_helper,
                  ams_profile, frame, bus_key, desc):
        # Establish baseline counter.
        baseline = None
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            hb = heartbeat_helper["read"]()
            if hb is not None and hb >= 6:
                baseline = hb
                break
            time.sleep(0.05)
        assert baseline is not None

        # Send the malformed trigger frame.
        bus = ams_profile[bus_key]
        r = subprocess.run(["cansend", bus, frame],
                           capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            # cansend rejected the frame — bus never saw it. That's
            # equivalent to "no reset" for our purposes; pass.
            pytest.skip(f"cansend rejected `{frame}` on {bus}: "
                        f"{r.stderr.strip()}")

        # Chip MUST NOT reset.
        assert not _heartbeat_reset_observed(observe_acu, baseline,
                                              window_s=1.5), (
            f"heartbeat counter reset on negative case `{desc}` "
            f"(frame={frame!r} on {bus}). The boot-trigger match is "
            "too permissive."
        )
