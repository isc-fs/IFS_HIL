"""
Block I — VCU firmware health & IWDG diagnosis (IFS08-CE-ECU#71).

The `0x704` health frame is the CAN-only IWDG instrument: emitted by DiagTask
@1 Hz (NOT ControlTask), so it survives a ControlTask stall — the exact failure
it diagnoses. Carries free_heap / min_free_heap / task-liveness bits /
reset_cause / uptime / last_fault. Decoders + enums in can_map.

I-004 (fault-sentinel latch across reset) needs a *forced* fault to latch
0x704.last_fault — deferred until there's a fault-injection hook.
"""
from __future__ import annotations

import time

from tools.firmware_test.vcu import can_map as M


def _wait_health(observe, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        f = observe.last(M.ID_PIT_HEALTH, extended=False)
        if f is not None:
            return M.decode_pit_health(f.data)
        time.sleep(0.02)
    return None


def _collect_health_stamps(observe, window_s):
    observe.clear()
    stamps, last = [], None
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        f = observe.last(M.ID_PIT_HEALTH, extended=False)
        if f is not None and f.timestamp != last:
            stamps.append(f.timestamp)
            last = f.timestamp
        time.sleep(0.01)
    return stamps


class TestI001HealthAt1Hz:

    def test_i001_health_present_and_cadenced(self, fresh_boot, pit_diag,
                                              observe_acu):
        """I-001: 0x704 present at ~1 Hz from DiagTask and decodes to a sane
        health struct (free_heap > 0)."""
        stamps = _collect_health_stamps(observe_acu, 3.6)
        assert len(stamps) >= 2, \
            f"only {len(stamps)} 0x704 frames in 3.6 s (expect ~3 @1 Hz)"
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert all(0.5 <= g <= 1.6 for g in gaps), \
            f"0x704 cadence off (want ~1 Hz): gaps {[round(g, 2) for g in gaps]}"
        h = _wait_health(observe_acu)
        assert h is not None and h["free_heap"] > 0, "0x704 free_heap zero/missing"


class TestI002ResetCause:

    def test_i002_reset_cause_real(self, fresh_boot, pit_diag, observe_acu):
        """I-002: reset_cause decodes to a defined ResetCause (not garbage).
        fresh_boot is a relay power-cut -> expect POR/PIN; the field must be a
        known cause either way."""
        h = _wait_health(observe_acu)
        assert h is not None, "no 0x704 health frame"
        known = {c.value for c in M.ResetCause}
        assert h["reset_cause"] in known, \
            f"reset_cause {h['reset_cause']} not a known ResetCause {known}"


class TestI003TaskLiveness:

    def test_i003_all_tasks_live(self, fresh_boot, inv_heartbeat, pit_diag,
                                 observe_acu):
        """I-003: all four task-liveness bits set — control / can_rx / can_tx /
        diag. CanRxTask only runs when frames arrive, so inv_heartbeat drives
        continuous 0x461 on the INV bus (the car's bus is never silent). DiagTask
        clears the mask each 1 Hz cycle, so OR the per-cycle masks over a window:
        each task must be seen running at least once."""
        seen = 0
        want = M.TASK_CONTROL | M.TASK_CAN_RX | M.TASK_CAN_TX | M.TASK_DIAG
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and (seen & want) != want:
            h = _wait_health(observe_acu, 1.5)
            if h is not None:
                seen |= h["task_ran_mask"]
            time.sleep(0.1)
        missing = want & ~seen
        assert missing == 0, \
            f"task-liveness (OR'd over window) 0x{seen:02X} missing bits " \
            f"0x{missing:02X} (want all of 0x{want:02X})"


class TestI005HeapStable:

    def test_i005_heap_no_slide(self, fresh_boot, pit_diag, observe_acu):
        """I-005: free_heap doesn't slide over a short soak (no leak), and
        min_free_heap <= free_heap (the watermark holds)."""
        h0 = _wait_health(observe_acu)
        assert h0 is not None and h0["free_heap"] > 0, "no/zero free_heap"
        assert h0["min_free_heap"] <= h0["free_heap"], \
            f"min_free_heap {h0['min_free_heap']} > free_heap {h0['free_heap']}"
        time.sleep(5.0)
        h1 = _wait_health(observe_acu)
        assert h1 is not None
        assert h1["free_heap"] >= h0["free_heap"] - 512, \
            f"free_heap slid {h0['free_heap']} -> {h1['free_heap']} over 5 s (leak?)"
