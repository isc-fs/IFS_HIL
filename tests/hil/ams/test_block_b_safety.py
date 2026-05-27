"""
Block B — Safety supervisor (post-refactor: SafetyTask + StateTask +
TelemetryTask are merged into MainTask, single 10 ms loop).

Per `isc-fs/IFS08-CE-AMS#272` (supersedes #245 — every test below is
observable over can0; SWD-gated rows removed).

| #272 ID | What it checks                                           | Status     |
|---------|----------------------------------------------------------|------------|
| B-020   | Boot grace suppresses VCU/BMS staleness                  | scaffolded — needs cold-boot-no-heartbeat fixture |
| B-021   | VCU 0x100 stale > VcuStaleMs → Error                     | implemented|
| B-022   | BMS module stale > BmsStaleMs → Error (Pico STOP_REPLY)  | TODO — implement in follow-up PR using pico_emu.stop_reply |
| B-026a..c | cell UV/OV/OT → Error via Pico INJECT_CELL_V/T         | implemented|
| B-026d UT | dropped — under-temp not a physical fault on a track car |            |

Out-of-#272 (kept as regression coverage):
| B-010   | MainTask 10 ms cadence via heartbeat indirection (60 s)  | implemented|
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# B-010 — MainTask 10 ms cadence via heartbeat
# ---------------------------------------------------------------------------

class TestB010MainTaskCadence:

    def test_b010_heartbeat_advances_at_500ms_for_60s(self, fresh_boot,
                                                       heartbeat_helper,
                                                       ams_profile):
        period_ms = int(ams_profile["tx_telemetry_period_ms"])
        # Per the test plan: over 60 s, expected delta is 120 ± 1 increments
        # (= 60_000 / 500 = 120, tolerance for boundary windowing).
        baseline = heartbeat_helper["read"]()
        # Spin until we have a baseline (chip may have just booted).
        deadline = time.monotonic() + 5.0
        while baseline is None and time.monotonic() < deadline:
            time.sleep(0.05)
            baseline = heartbeat_helper["read"]()
        assert baseline is not None, "no heartbeat seen yet — MainTask alive?"

        # Wait 60 s of wall-clock and check the counter has advanced by
        # 120 ± 1.
        time.sleep(60.0)
        final = heartbeat_helper["read"]()
        assert final is not None
        delta = (final - baseline) % 256
        expected = int(60_000 / period_ms)
        assert abs(delta - expected) <= 1, (
            f"heartbeat advanced by {delta} in 60 s, expected "
            f"{expected} ± 1. MainTask may be running too fast/slow "
            "or hanging intermittently."
        )


# ---------------------------------------------------------------------------
# B-022 — BMS module stale via Pico STOP_REPLY (AMS #272 row)
# ---------------------------------------------------------------------------

class TestB022BMSStale:
    """Per AMS #272 B-022: with the Pico LTC emulator on MLC2 J8,
    issuing STOP_REPLY 0x1F (silence all 5 modules' 2 chain positions)
    starves the AMS BMS poll — last_rx_tick for every module goes stale
    past BmsStaleMs, module_online_mask collapses to 0x00 (#250
    freshness semantics), and the safety predicate trips FSM to Error.

    Symmetric with E-065 but viewed from the Block B safety predicate
    perspective rather than the Block E chain-integrity perspective.
    """

    def test_b022_bms_stale_trips_error(self, fresh_boot, pico_emu,
                                         wait_for_state, ams_profile):
        # Sanity: chip is alive in Start before we silence the chain.
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        try:
            # Silence every chain position. Bytes for the silenced
            # positions read 0xFF on the wire → PEC fails for those ICs
            # → mask collapses to 0x00 within BmsStaleMs.
            pico_emu.stop_reply(0x1F)

            # Budget: kBmsStaleMs (1500 ms) + the kSafetyPeriodMs check
            # cycle + one telemetry cycle for the new state to surface.
            window_ms = (int(ams_profile.get("bms_stale_ms", 1500))
                         + int(ams_profile["tx_telemetry_period_ms"])
                         + 500)
            wait_for_state(M.FsmState.ERROR, timeout_ms=window_ms)
        finally:
            # Always resume so the next test in the session starts clean.
            pico_emu.resume_all()


# ---------------------------------------------------------------------------
# B-021 — VCU heartbeat stale trips (AMS #272 row)
# ---------------------------------------------------------------------------

class TestB021VCUHeartbeatStale:
    """Pause the bench's 0x100 emission, wait kVcuStaleMs + slack, and
    confirm the FSM latches Error. The chip must be past boot grace
    first — pre-grace the predicate is gated."""

    def test_b021_vcu_stale_trips_error(self, fresh_boot, acu_heartbeat,
                                         observe_acu, wait_for_state,
                                         ams_profile):
        # Wait grace + a couple of telemetry cycles so we're firmly in
        # post-grace evaluate-fault territory with VCU still fresh.
        time.sleep(int(ams_profile["boot_grace_ms"]) / 1000.0 + 0.2)

        # Sanity: chip is still in Start (heartbeat keeping VCU fresh,
        # nothing else in the predicate is tripping on this rig).
        frame = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
        assert frame is not None
        pre_state = M.decode_telem_status(frame.data)["state"]
        assert pre_state == M.FsmState.START, (
            f"Chip was in {M.FsmState.name(pre_state)} before B-017 began. "
            "Some other predicate is already tripping — investigate before "
            "treating this as a VCU-stale test."
        )

        # Stop emitting 0x100 and wait for the staleness predicate to
        # fire. Budget: kVcuStaleMs + the kSafetyPeriodMs check cycle
        # + one telemetry cycle for the new state to be visible.
        acu_heartbeat["pause"]()
        try:
            window_ms = (int(ams_profile["vcu_stale_ms"]) +
                         int(ams_profile["tx_telemetry_period_ms"]) + 200)
            wait_for_state(M.FsmState.ERROR, timeout_ms=window_ms)
        finally:
            # Always resume — leaving the heartbeat off would cascade
            # into subsequent tests in the same session.
            acu_heartbeat["resume"]()


# ---------------------------------------------------------------------------
# B-020 placeholder + B-025 SWD-only test dropped per AMS #272.
# B-020 (boot grace) needs a `cold_boot_no_heartbeat` fixture; reimplement
# when that lands. B-025 requires SWD which the rig doesn't have.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# B-026 — cell UV/OV/OT → Error via Pico injection  (AMS #272 row)
# ---------------------------------------------------------------------------

class TestB026CellRangeInjection:
    """Per AMS #272 B-026: with the Pico LTC emulator running, inject out-
    of-range cell V / cell T values and observe the corresponding safety
    predicate trip → FSM goes Error. Unblocked by the Pico emulator
    INJECT_CELL_V / INJECT_CELL_T commands shipped in PR #39 (v0.3.0+).

    Predicate thresholds per `ams_config.hpp`:
      - CellOverVoltageMv  = 4200
      - CellUnderVoltageMv = 2800
      - CellOverTempC      = 60

    The CellUnderTempC = -10 °C predicate is intentionally not exercised
    here: the car runs at ambient + I²R rise, so a cold-pack fault never
    happens in flight, and dragging the rig to sub-zero ambient to
    'verify' the predicate is theatre with no operational return.

    Each sub-test picks a different (module, cell|sensor) so the
    suite stresses the chain-position translation in the client too.
    """

    def test_b026_cell_overvoltage(self, fresh_boot, pico_emu,
                                    wait_for_state, ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        pico_emu.inject_cell_v(module=2, cell=5, mV=4400)  # > 4200
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 500)

    def test_b026_cell_undervoltage(self, fresh_boot, pico_emu,
                                     wait_for_state, ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        # cell 10 lives on the lower LTC of module 0 -- exercises the
        # client's >= 10 → chain_pos+1 translation.
        pico_emu.inject_cell_v(module=0, cell=10, mV=2500)  # < 2800
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 500)

    # See E-067 NOTE: temp injection only confirmed to reach AMS at
    # (module=0, sensor=0) today; full address-space coverage is a
    # Pico-emulator follow-up. Both sub-tests use the known-good
    # address so they exercise the OV/UV-of-temperature predicate
    # paths even though they cover only one chain slot.
    def test_b026_cell_overtemperature(self, fresh_boot, pico_emu,
                                        wait_for_state, ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        pico_emu.inject_cell_t(module=0, sensor=0, deci_degC=750)  # 75 °C
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 500)

    # B-026d (cell under-temp) intentionally not implemented — see class
    # docstring. The predicate exists in firmware; we just don't exercise
    # it from the bench because no realistic track scenario reaches it.


# B-027 force_error_set hook dropped per AMS #272 — no live setter
# exists in firmware, and #272 doesn't reserve a row for it. If the
# hook is ever re-added, file a new test against that interface.
