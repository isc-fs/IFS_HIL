"""
Block B — Safety supervisor (post-refactor: SafetyTask + StateTask +
TelemetryTask are merged into MainTask, single 10 ms loop).

Implements B-010..B-017 from `isc-fs/IFS08-CE-AMS#123`.

| Test  | What it checks                                            | Status     |
|-------|-----------------------------------------------------------|------------|
| B-010 | MainTask 10 ms cadence via heartbeat indirection (60 s)   | implemented|
| B-011 | IWDG resets the chip if MainTask hangs                    | needs GDB  |
| B-012 | Watchdog reset re-opens relays                            | needs GDB  |
| B-013 | ErrorLatch clears on boot under HIL_STUB                  | needs GDB  |
| B-014 | BMS predicates trip on cell UV/OV/OT                      | deferred (LTC chain) |
| B-015 | Current overlimit predicate trips → Error                 | needs PF7 stim  |
| B-016 | Current sensor stale → Error                              | needs GDB  |
| B-017 | VCU heartbeat stale → Error                               | implemented|
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
# B-011 — IWDG resets the chip if MainTask hangs
# ---------------------------------------------------------------------------

class TestB011IWDGOnMainTaskHang:

    @pytest.mark.skip(reason=(
        "B-011 requires GDB+OpenOCD to set a breakpoint inside the 10 ms "
        "MainTask loop and hold the chip past the IWDG window. No SWD "
        "interface on this rig's MLC. Open a follow-up issue when "
        "SWD/JTAG is wired to a slot."
    ))
    def test_b011_iwdg_resets_on_hang(self):
        pass


# ---------------------------------------------------------------------------
# B-012 — Watchdog reset re-opens relays
# ---------------------------------------------------------------------------

class TestB012WatchdogReopensRelays:

    @pytest.mark.skip(reason=(
        "B-012 requires (a) IWDG injection (GDB, see B-011) and "
        "(b) a logic-analyser probe on PB5/6/7 to observe the relay "
        "lines within milliseconds of the reset. Neither is available "
        "on the current rig."
    ))
    def test_b012_relays_reopen_after_iwdg(self):
        pass


# ---------------------------------------------------------------------------
# B-013 — ErrorLatch clears on boot under HIL_STUB
# ---------------------------------------------------------------------------

class TestB013ErrorLatchClearsOnBoot:

    @pytest.mark.skip(reason=(
        "B-013 requires GDB to write `RTC->BKP1R = 0xA115EE51` (the ERROR "
        "magic) before the power-cycle so we can observe whether "
        "App_InitTask::ErrorLatch::clear() wipes it. We can't poke "
        "backup-domain registers from outside without SWD. The "
        "auto-clear behaviour is exercised every time `fresh_boot` runs "
        "(every test in this suite starts from a clean BKP1R), so the "
        "clear is implicitly tested — this test just isolates it."
    ))
    def test_b013_latch_clears_under_hil_stub(self):
        pass


# ---------------------------------------------------------------------------
# B-014 — BMS predicates trip on cell UV/OV/OT
# ---------------------------------------------------------------------------

class TestB014BMSPredicates:

    @pytest.mark.skip(reason=(
        "B-014 is deferred until an LTC6811 isoSPI chain is present on "
        "the bench (per #123 'Deferred' section). The HIL_STUB seeder "
        "writes cell_mV=3750 / cell_tempC=25 deep inside the predicate "
        "windows, so no fault is reachable from the bench under this "
        "build flag. Repeat on a real-chain rig with a flight build."
    ))
    def test_b014_bms_predicates_trip(self):
        pass


# ---------------------------------------------------------------------------
# B-015 — Current overlimit predicate trips
# ---------------------------------------------------------------------------

class TestB015CurrentOverlimit:

    @pytest.mark.skip(reason=(
        "B-015 needs an analog source driving PF7 (ADC3 ch3) past "
        "kImaxMa worth of voltage (≈ 4.6 V offset from kCurrentZeroMv "
        "for 200 A, well over Vref). DAC2 ch0 is now wired to PF7 on "
        "the bench (per isc-fs/IFS08-CE-AMS#189), so this is unblocked "
        "as soon as current_heartbeat_dac_* keys land in "
        "ams_profile.yaml and the test body is rewritten to use the "
        "current_heartbeat fixture's set_mA(>= 200_000) helper. "
        "Tracked as B-024 in isc-fs/IFS08-CE-AMS#193."
    ))
    def test_b015_current_overlimit_trips_error(self):
        pass


# ---------------------------------------------------------------------------
# B-016 — Current sensor stale trips
# ---------------------------------------------------------------------------

class TestB016CurrentStale:

    @pytest.mark.skip(reason=(
        "B-016 requires halting CurrentSensorTask via GDB so its "
        "last_update_tick goes stale. No SWD on the rig."
    ))
    def test_b016_current_stale_trips_error(self):
        pass


# ---------------------------------------------------------------------------
# B-017 — VCU heartbeat stale trips
# ---------------------------------------------------------------------------

class TestB017VCUHeartbeatStale:
    """Pause the bench's 0x100 emission, wait kVcuStaleMs + slack, and
    confirm the FSM latches Error. The chip must be past boot grace
    first — pre-grace the predicate is gated."""

    def test_b017_vcu_stale_trips_error(self, fresh_boot, acu_heartbeat,
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
