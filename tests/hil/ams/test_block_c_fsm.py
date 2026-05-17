"""
Block C — FSM transitions.

Rewritten for `isc-fs/IFS08-CE-AMS#187` (TSMS + RST_PIL FSM). The CAN-
driven Start triggers (`0x600`, `0x18FF50E7`) were retired; the FSM now
gates on two physical GPIO inputs and distinguishes car-vs-charger by
VCU `0x100` heartbeat freshness captured at Start -> Precharge.

| Test  | What it checks                                                   | Status      |
|-------|------------------------------------------------------------------|-------------|
| C-020 | Start stays put with only TSMS or only RST_PIL                   | implemented |
| C-021 | Start -> Precharge on TSMS && RST_PIL                            | implemented |
| C-022 | Precharge -> Transition once DC bus hits 95% of pack             | implemented |
| C-023 | Transition -> Run after hold in car mode (VCU heartbeat fresh)   | implemented |
| C-024 | Precharge timeout -> Error                                       | implemented |
| C-025 | Run -> Error (sticky) on TSMS drop                               | implemented |
| C-026 | Transition -> Charge in charger mode (VCU heartbeat paused)      | implemented |
| C-027 | Error sticky within a boot                                       | implemented |
| C-028 | Error survives reset (flight semantics)                          | deferred    |

All tests skip cleanly when the cockpit fixture is unavailable
(cockpit_* keys absent from `ams_profile.yaml` -- happens until the
bench wires PF9/PF10 through the TCA9555).
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_cockpit(cockpit):
    if cockpit is None:
        pytest.skip("cockpit fixture unavailable -- fill cockpit_* keys in "
                    "ams_profile.yaml once PF9/PF10 are wired through "
                    "the TCA9555.")


def _drive_to_precharge(cockpit, wait_for_state, ams_profile):
    """Cockpit-asserted Start -> Precharge.  Returns the Precharge snapshot."""
    cockpit.assert_both()
    return wait_for_state(
        M.FsmState.PRECHARGE,
        timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


def _drive_to_run(cockpit, acu_heartbeat, wait_for_state, ams_profile):
    """Cockpit + ramped DC bus -> Run (car mode)."""
    _drive_to_precharge(cockpit, wait_for_state, ams_profile)

    # Pack voltage under stub = 356_250 mV -> 356.25 V. 95% target ~ 339 V.
    pack_V   = int(ams_profile["stub_expected_pack_mV"]) // 1000
    target_V = int(pack_V * 0.96)
    acu_heartbeat["set_volts"](target_V)

    wait_for_state(
        M.FsmState.TRANSITION,
        timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)

    hold_ms = int(ams_profile["transition_hold_ms"])
    return wait_for_state(
        M.FsmState.RUN,
        timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# C-020 -- Start stays put with only one of the two cockpit inputs
# ---------------------------------------------------------------------------

class TestC020CockpitGateRequiresBoth:

    def test_c020_tsms_only(self, fresh_boot, cockpit, wait_for_state,
                            observe_acu, ams_profile):
        _require_cockpit(cockpit)
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        cockpit.deassert_all()
        cockpit.assert_tsms()

        # Watch a full transition window; state must NOT leave Start.
        window_ms = int(ams_profile["state_transition_window_ms"]) + 100
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.START, (
                    f"FSM left Start with only TSMS asserted (now in "
                    f"{M.FsmState.name(state)}). Both TSMS and RST_PIL "
                    f"must be high to fire the gate.")
            time.sleep(0.02)

    def test_c020_rst_pil_only(self, fresh_boot, cockpit, wait_for_state,
                               observe_acu, ams_profile):
        _require_cockpit(cockpit)
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        cockpit.deassert_all()
        cockpit.assert_rst_pil()

        window_ms = int(ams_profile["state_transition_window_ms"]) + 100
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.START, (
                    f"FSM left Start with only RST_PIL asserted (now in "
                    f"{M.FsmState.name(state)}).")
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# C-021 -- Start -> Precharge on TSMS && RST_PIL
# ---------------------------------------------------------------------------

class TestC021StartToPrecharge:

    def test_c021(self, fresh_boot, cockpit, wait_for_state, ams_profile):
        _require_cockpit(cockpit)
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        cockpit.assert_both()
        snap = wait_for_state(
            M.FsmState.PRECHARGE,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        assert snap["state"] == M.FsmState.PRECHARGE


# ---------------------------------------------------------------------------
# C-022 -- Precharge -> Transition on DC bus target
# ---------------------------------------------------------------------------

class TestC022PrechargeToTransition:

    def test_c022(self, fresh_boot, cockpit, acu_heartbeat,
                  wait_for_state, ams_profile):
        _require_cockpit(cockpit)
        _drive_to_precharge(cockpit, wait_for_state, ams_profile)

        pack_V   = int(ams_profile["stub_expected_pack_mV"]) // 1000
        target_V = int(pack_V * 0.96)
        acu_heartbeat["set_volts"](target_V)

        wait_for_state(
            M.FsmState.TRANSITION,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# C-023 -- Transition -> Run after hold (car mode)
# ---------------------------------------------------------------------------

class TestC023TransitionToRun:

    def test_c023(self, fresh_boot, cockpit, acu_heartbeat,
                  wait_for_state, ams_profile):
        _require_cockpit(cockpit)
        run = _drive_to_run(cockpit, acu_heartbeat, wait_for_state, ams_profile)
        assert run["state"] == M.FsmState.RUN


# ---------------------------------------------------------------------------
# C-024 -- Precharge timeout -> Error
# ---------------------------------------------------------------------------

class TestC024PrechargeTimeout:

    def test_c024(self, fresh_boot, cockpit, acu_heartbeat,
                  wait_for_state, ams_profile):
        _require_cockpit(cockpit)
        _drive_to_precharge(cockpit, wait_for_state, ams_profile)
        # Heartbeat stays at default 0 V -- precharge never reaches target.

        window_ms = (int(ams_profile["precharge_max_ms"]) +
                     int(ams_profile["state_transition_window_ms"]) + 200)
        wait_for_state(M.FsmState.ERROR, timeout_ms=window_ms)


# ---------------------------------------------------------------------------
# C-025 -- Run -> Error (latched) on TSMS drop
# ---------------------------------------------------------------------------
# Operator chose conservative semantics in PR #187: every AIR-open event
# is a sticky fault requiring power-cycle. Run / Charge no longer have
# a "clean shutdown back to Start" path.

class TestC025RunToErrorOnCockpitDrop:

    def test_c025_tsms_drop(self, fresh_boot, cockpit, acu_heartbeat,
                            wait_for_state, ams_profile):
        _require_cockpit(cockpit)
        _drive_to_run(cockpit, acu_heartbeat, wait_for_state, ams_profile)

        cockpit.deassert_tsms()
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


# ---------------------------------------------------------------------------
# C-026 -- Transition -> Charge in charger mode (VCU heartbeat paused)
# ---------------------------------------------------------------------------
# Car-vs-charger is captured at Start -> Precharge from VCU 0x100
# heartbeat freshness. Pause the heartbeat first, wait > kVcuFreshMs,
# then assert cockpit -> mode locks to Charger. After the lock fires the
# heartbeat can resume (or a oneshot bump) so precharge actually
# completes; the locked mode does NOT re-evaluate.

class TestC026TransitionToCharge:

    def test_c026(self, fresh_boot, cockpit, acu_heartbeat, acu_stim,
                  wait_for_state, ams_profile):
        _require_cockpit(cockpit)

        # Silence the VCU heartbeat for > kVcuFreshMs (1 s in firmware).
        acu_heartbeat["pause"]()
        # Conservative wait: 1 s (kVcuFreshMs) + safety margin.
        time.sleep(1.2)

        # Cockpit gate -> Start->Precharge with mode_locked = Charger.
        cockpit.assert_both()
        wait_for_state(
            M.FsmState.PRECHARGE,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)

        # Mode already locked; safe to drive DC bus high so precharge
        # completes. Use a one-shot send so we don't accidentally
        # re-prime the freshness window before the lock decision (the
        # decision has already been made, so it doesn't matter -- but
        # being explicit keeps the intent clear).
        pack_V   = int(ams_profile["stub_expected_pack_mV"]) // 1000
        target_V = int(pack_V * 0.96)
        acu_stim.send_dc_bus_v(target_V)

        wait_for_state(
            M.FsmState.TRANSITION,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)

        # Hold elapses -> Charge (not Run, because mode_locked = Charger).
        hold_ms = int(ams_profile["transition_hold_ms"])
        wait_for_state(
            M.FsmState.CHARGE,
            timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# C-027 -- Error sticky within a boot
# ---------------------------------------------------------------------------

class TestC027ErrorSticky:

    def test_c027(self, fresh_boot, acu_heartbeat, wait_for_state,
                  observe_acu, ams_profile):
        # Trip Error via VCU staleness (same path as B-017). Other fault
        # paths (current overlimit) need PF11 stim we don't have.
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
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            assert f is not None
            state = M.decode_telem_status(f.data)["state"]
            assert state == M.FsmState.ERROR, (
                f"Error was not sticky: chip transitioned back to "
                f"{M.FsmState.name(state)} after the heartbeat resumed.")
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# C-028 -- Error survives reset (flight semantics)
# ---------------------------------------------------------------------------

class TestC028ErrorSurvivesReset:

    @pytest.mark.skip(reason=(
        "C-028 needs a flight build (no -DAMS_BMS_HIL_STUB) -- the stub "
        "build's App_InitTask explicitly clears ErrorLatch on every boot, "
        "which is the inverse of what this test asserts. A flight build "
        "on this rig would latch ERROR immediately on the missing LTC "
        "chain, so C-028 is doubly blocked. Defer until the bench has "
        "an LTC chain and we can run a flight build that boots clean."))
    def test_c028_error_survives_reset(self):
        pass
