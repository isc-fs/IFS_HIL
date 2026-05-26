"""
Block B — Safety supervisor (post-refactor: SafetyTask + StateTask +
TelemetryTask are merged into MainTask, single 10 ms loop).

Per `isc-fs/IFS08-CE-AMS#245` (supersedes the v1.4.0 B-010..B-017
scheme from #123). Tests below renumbered to match the #245 spec;
the original B-010..B-013 rows are kept as out-of-#245-scope
regression coverage (cadence + IWDG + latch contract).

| #245 ID | What it checks                                           | Status     |
|---------|----------------------------------------------------------|------------|
| B-020   | Boot grace suppresses VCU/BMS staleness                  | scaffolded |
| B-021   | VCU 0x100 stale > VcuStaleMs → Error (unblocked by #243) | implemented|
| B-022   | BMS module stale > BmsStaleMs → Error (Pico STOP_REPLY)  | scaffolded |
| B-023   | Current sensor stale > IStaleMs → Error                  | deferred — flight build only |
| B-024   | Current overlimit (|filtered_mA| > CurrentMaxMa) → Error | needs DAC4 ch0 stim |
| B-025   | sensor_fault from ADC failure → Error                    | scaffolded — needs SWD |
| B-026   | cell UV/OV/OT/UT → Error via Pico INJECT_CELL_V/T        | scaffolded |
| B-027   | force_error_set legacy hook                              | deferred — no consumer |

Out-of-#245 retained:
| B-010   | MainTask 10 ms cadence via heartbeat indirection (60 s)  | implemented|
| B-011   | IWDG resets the chip if MainTask hangs                   | needs GDB  |
| B-012   | Watchdog reset re-opens relays                           | needs GDB  |
| B-013   | ErrorLatch clears on boot under HIL_STUB                 | needs GDB  |
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

class TestB022BMSStale:

    @pytest.mark.skip(reason=(
        "B-014 is deferred until an LTC6811 isoSPI chain is present on "
        "the bench (per #123 'Deferred' section). The HIL_STUB seeder "
        "writes cell_mV=3750 / cell_tempC=25 deep inside the predicate "
        "windows, so no fault is reachable from the bench under this "
        "build flag. Repeat on a real-chain rig with a flight build."
    ))
    def test_b022_bms_stale_trips_error(self):
        pass


# ---------------------------------------------------------------------------
# B-015 — Current overlimit predicate trips
# ---------------------------------------------------------------------------

class TestB024CurrentOverlimit:

    @pytest.mark.skip(reason=(
        "B-015 needs an analog source driving PF11 (ADC1 ch2) past "
        "kImaxMa worth of voltage (≈ 4.6 V offset from kCurrentZeroMv "
        "for 200 A, well over Vref). The bench's DAC80504 outputs are "
        "not currently routed to SLOT1_AN0 (= GPIO7 = PF11). When the "
        "bench respin wires DAC2_OUT0 → SLOT1_AN0, this test becomes "
        "directly runnable."
    ))
    def test_b024_current_overlimit_trips_error(self):
        pass


# ---------------------------------------------------------------------------
# B-016 — Current sensor stale trips
# ---------------------------------------------------------------------------

class TestB023CurrentStale:

    @pytest.mark.skip(reason=(
        "B-016 requires halting CurrentSensorTask via GDB so its "
        "last_update_tick goes stale. No SWD on the rig."
    ))
    def test_b023_current_stale_trips_error(self):
        pass


# ---------------------------------------------------------------------------
# B-017 — VCU heartbeat stale trips
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
# B-020 — Boot grace suppresses staleness predicates  (NEW per #245)
# ---------------------------------------------------------------------------

import pytest as _pytest  # noqa: E402

class TestB020BootGraceSuppressesStaleness:
    """During the first SafetyBootGraceMs (= 2 s) after reset, the
    safety supervisor must NOT trip on staleness predicates even when
    they're objectively true (no VCU heartbeat, no current sensor
    update, etc.). This is the grace window that lets BL hand-off +
    app-init complete before the safety net starts running."""

    @_pytest.mark.skip(reason=(
        "Needs a fixture that powers MLC2 with NO heartbeats (acu_heartbeat "
        "paused, current_heartbeat paused) and observes that 0x4A0[0] stays "
        "in Start for the full SafetyBootGraceMs before tripping to Error. "
        "fresh_boot starts both heartbeats so it can't be used as-is. "
        "Implement once a 'cold_boot_no_heartbeat' fixture lands."
    ))
    def test_b020_boot_grace_suppresses_staleness(self):
        pass


# ---------------------------------------------------------------------------
# B-025 — sensor_fault from ADC failure → Error  (NEW per #245)
# ---------------------------------------------------------------------------

class TestB025SensorFault:
    @_pytest.mark.skip(reason=(
        "Needs a way to force the STM32's ADC into a fault state -- SWD "
        "attach + GDB to corrupt the calibration register, or a hardware "
        "stim that drives the ADC input out of its measurable range. "
        "Neither exists on this rig today."
    ))
    def test_b025_sensor_fault_trips_error(self):
        pass


# ---------------------------------------------------------------------------
# B-026 — cell UV/OV/OT/UT → Error via Pico injection  (NEW per #245)
# ---------------------------------------------------------------------------

class TestB026CellRangeInjection:
    """Per #245: with the Pico LTC emulator running, inject out-of-
    range cell V / cell T values and observe the corresponding
    safety predicate trip → FSM goes Error. Unblocked by the Pico
    emulator INJECT_CELL_V / INJECT_CELL_T commands shipped in
    PR #39 (v0.3.0+).

    Predicate thresholds per `ams_config.hpp`:
      - CellOverVoltageMv  = 4200
      - CellUnderVoltageMv = 2800
      - CellOverTempC      = 60
      - CellUnderTempC     = -10

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

    def test_b026_cell_undertemperature(self, fresh_boot, pico_emu,
                                         wait_for_state, ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        pico_emu.inject_cell_t(module=0, sensor=0, deci_degC=-200)  # -20 °C
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 500)


# ---------------------------------------------------------------------------
# B-027 — force_error_set legacy hook  (DEFERRED per #245)
# ---------------------------------------------------------------------------

class TestB027ForceErrorSetHook:
    @_pytest.mark.skip(reason=(
        "Per #245: no live setter exists for force_error_set. Test "
        "DEFERRED until firmware exposes one (would be a #if HIL-only "
        "diag-write CAN command). Tracking row only."
    ))
    def test_b027_force_error_set(self):
        pass
