"""
Block C — FSM transitions.

Implements C-020..C-028 from `isc-fs/IFS08-CE-AMS#123`. All tests drive
the FSM by injecting frames on FDCAN1 (`can0`) and observe the
resulting state byte in the `0x4A0` telemetry stream.

| Test  | What it checks                                          | Status      |
|-------|---------------------------------------------------------|-------------|
| C-020 | Start → Precharge on start button                       | implemented |
| C-021 | Start → Charge on charger detect                        | implemented |
| C-022 | Precharge → Transition on DC bus target                 | implemented |
| C-023 | Transition → Run after hold window                      | implemented |
| C-024 | Precharge timeout → Error                               | implemented |
| C-025 | Run is terminal (charger toggle ignored)                | implemented |
| C-026 | Charge is terminal (charger drop ignored)               | implemented |
| C-027 | Error sticky within a boot                              | implemented |
| C-028 | Error survives reset (flight semantics)                 | deferred    |
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Helpers — drive the chip into a known state from fresh_boot
# ---------------------------------------------------------------------------

def _drive_to_precharge(acu_stim, wait_for_state, ams_profile):
    """From Start, press the start button. Returns when state == Precharge."""
    acu_stim.send_start_button(pressed=True)
    return wait_for_state(M.FsmState.PRECHARGE,
                          timeout_ms=int(ams_profile["state_transition_window_ms"]))


def _drive_to_run(acu_stim, acu_heartbeat, wait_for_state, ams_profile):
    """From Start, button → Precharge → ramp DC bus → Transition → hold → Run."""
    _drive_to_precharge(acu_stim, wait_for_state, ams_profile)

    # Pack voltage under stub = 356_250 mV → 356.25 V. 95% target ≈ 339 V.
    # Push the heartbeat target above that to satisfy precharge_target_reached.
    pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
    target_V = int(pack_V * 0.96)  # comfortably above the 95% gate
    acu_heartbeat["set_volts"](target_V)

    # Wait for Transition (chip should react within the safety period).
    wait_for_state(M.FsmState.TRANSITION,
                   timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)

    # Hold the DC bus for kTransitionHoldMs + slack, then expect Run.
    hold_ms = int(ams_profile["transition_hold_ms"])
    return wait_for_state(M.FsmState.RUN,
                          timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# C-020 — Start → Precharge on start button
# ---------------------------------------------------------------------------

class TestC020StartToPrecharge:

    def test_c020(self, fresh_boot, acu_stim, wait_for_state, ams_profile):
        # Ensure we're in Start before the stim. fresh_boot guarantees
        # the first frame was Start; verify nothing else fired before
        # we send the button press.
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        # Issue start button. Standard ID 0x600, payload 0x01.
        acu_stim.send_start_button(pressed=True)
        snap = wait_for_state(M.FsmState.PRECHARGE,
                              timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        assert snap["state"] == M.FsmState.PRECHARGE


# ---------------------------------------------------------------------------
# C-021 — Start → Charge on charger detect
# ---------------------------------------------------------------------------

class TestC021StartToCharge:

    def test_c021(self, fresh_boot, acu_stim, wait_for_state, ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        # Extended ID 0x18FF50E7, any payload — VehicleService::update_from_frame
        # passes the frame through to CurrentService::set_charger_detected(true).
        acu_stim.send_charger_detect()
        wait_for_state(M.FsmState.CHARGE,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


# ---------------------------------------------------------------------------
# C-022 — Precharge → Transition on DC bus target
# ---------------------------------------------------------------------------

class TestC022PrechargeToTransition:

    def test_c022(self, fresh_boot, acu_stim, acu_heartbeat,
                  wait_for_state, ams_profile):
        # Get into Precharge first.
        _drive_to_precharge(acu_stim, wait_for_state, ams_profile)

        # Bump heartbeat DC bus voltage above the 95% target.
        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        target_V = int(pack_V * 0.96)
        acu_heartbeat["set_volts"](target_V)

        wait_for_state(M.FsmState.TRANSITION,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# C-023 — Transition → Run after hold window
# ---------------------------------------------------------------------------

class TestC023TransitionToRun:

    def test_c023(self, fresh_boot, acu_stim, acu_heartbeat,
                  wait_for_state, ams_profile):
        run = _drive_to_run(acu_stim, acu_heartbeat, wait_for_state, ams_profile)
        assert run["state"] == M.FsmState.RUN


# ---------------------------------------------------------------------------
# C-024 — Precharge timeout → Error
# ---------------------------------------------------------------------------

class TestC024PrechargeTimeout:

    def test_c024(self, fresh_boot, acu_stim, acu_heartbeat,
                  wait_for_state, ams_profile):
        # Get into Precharge but keep DC bus voltage at the quiescent
        # value (well below the 95% target). Wait kPrechargeMaxMs + slack.
        _drive_to_precharge(acu_stim, wait_for_state, ams_profile)
        # heartbeat stays at the default quiescent voltage (0 V) — never reaches target.

        window_ms = (int(ams_profile["precharge_max_ms"]) +
                     int(ams_profile["state_transition_window_ms"]) + 200)
        wait_for_state(M.FsmState.ERROR, timeout_ms=window_ms)


# ---------------------------------------------------------------------------
# C-025 — Run is terminal (charger toggle ignored)
# ---------------------------------------------------------------------------

class TestC025RunTerminal:

    def test_c025(self, fresh_boot, acu_stim, acu_heartbeat,
                  wait_for_state, observe_acu, ams_profile):
        _drive_to_run(acu_stim, acu_heartbeat, wait_for_state, ams_profile)

        # Send a charger-detect frame while in Run. State must stay Run.
        acu_stim.send_charger_detect()

        # Watch a full state-transition window; state must not change.
        deadline = time.monotonic() + (int(ams_profile["state_transition_window_ms"]) + 100) / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.RUN, (
                    f"Run was disturbed by charger-detect: now in "
                    f"{M.FsmState.name(state)}. Run is supposed to be terminal."
                )
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# C-026 — Charge is terminal (charger drop ignored)
# ---------------------------------------------------------------------------

class TestC026ChargeTerminal:

    def test_c026(self, fresh_boot, acu_stim, wait_for_state, observe_acu,
                  ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        acu_stim.send_charger_detect()
        wait_for_state(M.FsmState.CHARGE,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)

        # There's no "charger absent" frame — CurrentService.set_charger_detected
        # is set sticky from the 0x18FF50E7 frame. Nothing in the bench can
        # un-set it short of a power-cycle, which is what Charge being
        # terminal means: even if we stop sending charger frames, we
        # stay in Charge.
        deadline = time.monotonic() + (int(ams_profile["state_transition_window_ms"]) + 500) / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.CHARGE, (
                    f"Charge was disturbed: now in {M.FsmState.name(state)}. "
                    "Charge is supposed to be terminal."
                )
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# C-027 — Error sticky within a boot
# ---------------------------------------------------------------------------

class TestC027ErrorSticky:

    def test_c027(self, fresh_boot, acu_heartbeat, wait_for_state,
                  observe_acu, ams_profile):
        # Trip Error via VCU staleness (same path as B-017). Other
        # fault paths (current overlimit) need PF11 stim we don't have.
        time.sleep(int(ams_profile["boot_grace_ms"]) / 1000.0 + 0.2)

        acu_heartbeat["pause"]()
        try:
            window_ms = (int(ams_profile["vcu_stale_ms"]) +
                         int(ams_profile["tx_telemetry_period_ms"]) + 200)
            wait_for_state(M.FsmState.ERROR, timeout_ms=window_ms)
        finally:
            acu_heartbeat["resume"]()

        # Heartbeat is back on, predicate is no longer tripping. If
        # Error is sticky, state stays at Error for at least 5 s.
        # Per the test plan: 5 s budget.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            assert f is not None
            state = M.decode_telem_status(f.data)["state"]
            assert state == M.FsmState.ERROR, (
                f"Error was not sticky: chip transitioned back to "
                f"{M.FsmState.name(state)} after the heartbeat resumed."
            )
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# C-028 — Error survives reset (flight semantics)
# ---------------------------------------------------------------------------

class TestC028ErrorSurvivesReset:

    @pytest.mark.skip(reason=(
        "C-028 needs a flight build (no -DAMS_BMS_HIL_STUB) — the stub "
        "build's App_InitTask explicitly clears ErrorLatch on every boot, "
        "which is the inverse of what this test asserts. A flight build "
        "on this rig would latch ERROR immediately on the missing LTC "
        "chain, so C-028 is doubly blocked. Defer until the bench has "
        "an LTC chain and we can run a flight build that boots clean."
    ))
    def test_c028_error_survives_reset(self):
        pass
