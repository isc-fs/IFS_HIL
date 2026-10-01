"""
Block J — FDCAN1 bus-off recovery (0x6C9), AMS v1.6.2 (#391 / #392).

The STM32H7 M_CAN peripheral latches CCCR.INIT on sustained TX errors
(bus-off), halting both TX and RX. AMS PR #391 polls the protocol status
and, on a latched bus-off, cycles HAL_FDCAN_Stop/Start (gated by
FdcanBusOffRetryMs) to recover. 0x6C9 reports busoff_recovery_count +
acu_tx_fail_count on the pit-diag stream.

| ID    | What it checks                                            | Runs on   |
|-------|----------------------------------------------------------|-----------|
| J-130 | A real FDCAN1 bus-off is recovered; telemetry resumes    | USB-CAN   |
| J-131 | busoff_recovery_count increments by >=1 per recovery     | USB-CAN   |
| J-132 | NO spurious recovery on a healthy bus (count stays put)  | std rig   |

J-130/131 need a *second* CAN interface on the AMS FDCAN1 bus to inject the
bus-off (a bitrate-mismatch flood). The bench mcp251x wedges to ERROR under
the listen-only reconfiguration that injection requires (#392), so they are
gated on AMS_BUSOFF_IFACE (a USB-CAN adapter) and otherwise skip — the
recovery *logic* is covered host-side in SIL. J-132 needs no injection and
runs on the standard rig: it is the regression guard that the recovery poll
does NOT fire on a quiet, healthy bus (the failure mode a naive poll invites).
"""

from __future__ import annotations

import os
import time
import pytest

from tools.firmware_test.ams import can_map as M

# A USB-CAN adapter on the AMS FDCAN1 bus, e.g. "can3" — set only once the
# adapter + the _inject_busoff helper are wired at bench bring-up (#392).
_BUSOFF_IFACE = os.environ.get("AMS_BUSOFF_IFACE")
_GATE = pytest.mark.skipif(
    not _BUSOFF_IFACE,
    reason="needs AMS_BUSOFF_IFACE (a USB-CAN adapter on the FDCAN1 bus): the "
           "bench mcp251x wedges under the bus-off injection (#392); the "
           "recovery logic is covered host-side in SIL")


def _recovery(pit_diag) -> dict:
    """0x6C9 decoded, or skip cleanly if the frame is absent (pre-#391)."""
    pit_diag.wait_for_scan()
    try:
        data = pit_diag.wait_for(M.ID_PIT_DIAG_FDCAN1_RECOVERY)
    except AssertionError:
        pytest.skip("no 0x6C9 FDCAN1-recovery frame -- firmware predates #391")
    return M.decode_fdcan1_recovery(data)


def _inject_busoff(iface: str, ams_profile) -> None:
    """Force an FDCAN1 bus-off by flooding `iface` at a mismatched bitrate.

    Bench-specific: a USB-CAN adapter on the same physical bus, opened at the
    wrong bitrate, drives the AMS TEC past 255. This path is OUTSIDE the
    broker — a separate physical interface, not the mcp251x carrier bus — so
    it does not touch the six broker invariants. Implementation lands with
    the USB-CAN bring-up; until then AMS_BUSOFF_IFACE stays unset and the
    gated tests skip before reaching here."""
    raise NotImplementedError(
        "bus-off injection via USB-CAN is a bench bring-up item (#392); set "
        "AMS_BUSOFF_IFACE only once the adapter + this helper are wired")


class TestBlockJBusOff:

    # J-132 — runnable: the recovery poll must not false-fire ----------------
    def test_j132_no_spurious_recovery_healthy_bus(self, fresh_boot, pit_diag):
        before = _recovery(pit_diag)["busoff_recovery_count"]
        # watch a quiet, healthy bus across several pit-diag scans
        time.sleep(M.PIT_DIAG_SCAN_PERIOD_MS / 1000 * 5 + 1)
        after = _recovery(pit_diag)["busoff_recovery_count"]
        assert after == before, (
            f"#392: busoff_recovery_count moved {before} -> {after} on a "
            "healthy bus -- the recovery poll must only fire on a real bus-off.")

    # J-130 — USB-CAN-gated: a real bus-off is recovered ---------------------
    @_GATE
    def test_j130_busoff_is_recovered(self, fresh_boot, pit_diag, observe_acu,
                                      ams_profile):
        assert observe_acu.last(M.ID_TELEM_STATUS) is not None, "no telemetry pre-inject"
        _inject_busoff(_BUSOFF_IFACE, ams_profile)
        retry_ms = int(ams_profile.get("fdcan_busoff_retry_ms", 100))
        deadline = time.monotonic() + retry_ms / 1000 + 3.0
        recovered = False
        while time.monotonic() < deadline:
            t = time.monotonic()
            time.sleep(0.3)
            if observe_acu.count(M.ID_TELEM_STATUS, since=t) > 0:
                recovered = True
                break
        assert recovered, "FDCAN1 telemetry did not resume after an injected bus-off"

    # J-131 — USB-CAN-gated: recovery is counted -----------------------------
    @_GATE
    def test_j131_recovery_count_increments(self, fresh_boot, pit_diag, ams_profile):
        before = _recovery(pit_diag)["busoff_recovery_count"]
        _inject_busoff(_BUSOFF_IFACE, ams_profile)
        time.sleep(int(ams_profile.get("fdcan_busoff_retry_ms", 100)) / 1000 + 2.0)
        after = _recovery(pit_diag)["busoff_recovery_count"]
        assert after >= before + 1, (
            f"busoff_recovery_count {before} -> {after}: expected >= +1 after a bus-off")
