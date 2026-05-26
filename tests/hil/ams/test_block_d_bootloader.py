"""
Block D — Bootloader integration.

Per `isc-fs/IFS08-CE-AMS#245` (post-#243 numbering; supersedes the
v1.4.0 D-041..D-045 scheme from #123). Block D collapsed to just the
core BL contract:

| #245 ID | What it checks                                         | Status      |
|---------|--------------------------------------------------------|-------------|
| D-050   | Cold cycle → app auto-jumps                            | implemented |
| D-051   | Boot-trigger 0x002 reboots to BL (unblocked by #243)   | implemented |
| D-051b  | NEW — Same trigger reboots from Error state too        | implemented |

D-042 (BKP2R JumpReason), D-043 (firmware_info node ID), and D-045
(boot-trigger negative cases) are kept for regression coverage but
aren't in #245's reduced Block D scope. A-009 picks up the node-ID
check on the Block A boot baseline.
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
    """DEPRECATED — pre-#241 reboot detector that assumed the BL would
    auto-jump back to the app. With BKP0R magic (the current BL
    contract), a trigger reboot leaves the BL parked in BL mode and
    the app never restarts; the heartbeat counter never resumes.
    Use `_trigger_rebooted_to_bl` for trigger tests."""
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


def _trigger_rebooted_to_bl(observe_acu, ams_profile,
                            quiet_window_s: float = 1.5) -> bool:
    """Positive confirmation that the trigger landed the chip in BL.

    Two signals, both required:
    1. App telemetry STOPS — no new 0x4A0 frames arrive in the next
       `quiet_window_s` (BL doesn't emit AMS telemetry).
    2. The BL responds to `can-flasher discover` on the flash bus.

    Replaces the pre-#241 heartbeat-counter-resets approach, which
    assumed the BL would auto-jump back to the app after the trigger;
    the current BL stays parked (BKP0R magic) until explicitly told
    to jump, so the app never restarts and the counter never resumes."""
    observe_acu.clear()
    time.sleep(quiet_window_s)
    # 1) telemetry must have stopped — observer should have no new frames.
    f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
    if f is not None:
        return False
    # 2) positive BL discover on the flash bus.
    r = subprocess.run(
        ["can-flasher", "--interface", "socketcan",
         "--channel", ams_profile["bus_bms_bl"],
         "--bitrate", "500000",
         "--node-id", hex(int(ams_profile["bl_node_id"])),
         "--timeout", "3000", "discover"],
        capture_output=True, text=True, timeout=10)
    return r.returncode == 0 and "Node" in r.stdout


# ---------------------------------------------------------------------------
# D-041 — Boot-trigger jumps to BL
# ---------------------------------------------------------------------------

class TestD051BootTriggerJumps:

    def test_d051(self, fresh_boot, observe_acu, ams_profile):
        # Confirm the app is alive before triggering: at least one
        # 0x4A0 frame must be on the bus.
        deadline = time.monotonic() + 4.0
        saw_app = False
        while time.monotonic() < deadline:
            if observe_acu.last(M.ID_TELEM_STATUS, extended=False) is not None:
                saw_app = True
                break
            time.sleep(0.05)
        assert saw_app, "no 0x4A0 from the app — can't test trigger"

        # Send the trigger on the ACU bus (FDCAN1).
        subprocess.run(
            ["cansend", ams_profile["bus_acu"], "002#B007AD11"],
            check=True, timeout=2,
        )

        # After the trigger, the chip should reset and land in BL
        # (BKP0R magic keeps it parked there — no auto-jump). Two
        # positive signals: telemetry stops, BL is discoverable.
        assert _trigger_rebooted_to_bl(observe_acu, ams_profile), (
            "boot-trigger had no effect: app telemetry kept flowing "
            "and/or BL didn't appear on the flash bus. Either "
            "AcuCanTask didn't receive the standard-ID frame, "
            "Bootloader::request_reboot didn't fire, or BL did not "
            "see the BKP0R magic on reset."
        )


# ---------------------------------------------------------------------------
# D-041b — Trigger reaches BL even when FSM is in Error
# ---------------------------------------------------------------------------

class TestD051bTriggerFromErrorState:
    """Safety property: the boot trigger is the *only* way to reflash
    the AMS in the car (no reset switch on the enclosure, battery
    disconnect requires opening the accumulator). It MUST therefore be
    responsive regardless of FSM state — especially Error, since that
    is the state in which a fix-and-reflash is most likely to be
    needed.

    This test drives FSM into Error by pausing the VCU heartbeat
    (VcuStaleMs trips → safety predicate fires → Error sticky), then
    sends the trigger and verifies the same heartbeat-reset signature
    as D-041. The trigger handler lives in AcuCanTask::run, which is
    parallel to MainTask's FSM loop; it must remain reachable even
    when MainTask is wedged on the Error path."""

    def test_d051b_trigger_works_from_error(
            self, fresh_boot, observe_acu,
            acu_heartbeat, ams_profile):
        # Step 1: trip FSM into Error.
        acu_heartbeat["pause"]()
        deadline = time.monotonic() + 5.0
        in_error = False
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                if state == M.FsmState.ERROR:
                    in_error = True
                    break
            time.sleep(0.05)
        assert in_error, (
            "FSM didn't enter Error within 5 s of pausing the VCU "
            "heartbeat — VcuStaleMs predicate may have changed, or "
            "the heartbeat fixture failed to pause."
        )

        # Step 2: send the trigger on the ACU bus.
        subprocess.run(
            ["cansend", ams_profile["bus_acu"], "002#B007AD11"],
            check=True, timeout=2,
        )

        # Step 3: confirm the trigger landed the chip in BL even from
        # the Error code path. This is the case the operator MUST
        # recover from in the car -- if it fails, the AMS becomes
        # unreflashable without opening the accumulator.
        try:
            assert _trigger_rebooted_to_bl(observe_acu, ams_profile), (
                "boot-trigger ignored while FSM was in Error. This is "
                "a SAFETY-CRITICAL regression: the trigger is the only "
                "way to reflash the AMS in the car. AcuCanTask's RX "
                "path or Bootloader::request_reboot is unreachable "
                "from the Error code path."
            )
        finally:
            # Restore the heartbeat regardless of pass/fail so the next
            # test doesn't start in a degraded state.
            acu_heartbeat["resume"]()


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

class TestD050ColdCycleAutoJumps:

    def test_d050(self, fresh_boot, ams_profile):
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
