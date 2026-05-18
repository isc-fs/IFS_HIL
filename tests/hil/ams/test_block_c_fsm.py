"""
Block C — FSM transitions.

Rewritten for `isc-fs/IFS08-CE-AMS#187` (TSMS + DASH_CHG FSM). The CAN-
driven Start triggers (`0x600`, `0x18FF50E7`) were retired; the FSM now
gates on two physical GPIO inputs and distinguishes car-vs-charger by
VCU `0x100` heartbeat freshness captured at Start -> Precharge.

TSMS lives on the side of the car (external operator master switch);
DASH_CHG is the cockpit dashboard / charger button. They're driven by
two independent pytest fixtures (`tsms`, `dash_chg`) -- no `cockpit`
abstraction.

| Test  | What it checks                                                   | Status      |
|-------|------------------------------------------------------------------|-------------|
| C-020 | Start stays put with only TSMS or only DASH_CHG                  | implemented |
| C-021 | Start -> Precharge on TSMS && DASH_CHG                           | implemented |
| C-022 | Precharge -> Transition once DC bus hits 95% of pack             | implemented |
| C-023 | Transition -> Run after hold in car mode (VCU heartbeat fresh)   | implemented |
| C-024 | Precharge timeout -> Error                                       | implemented |
| C-025 | Run -> Error (sticky) on TSMS drop                               | implemented |
| C-026 | Transition -> Charge in charger mode (VCU heartbeat paused)      | implemented |
| C-027 | Error sticky within a boot                                       | implemented |
| C-028 | Error survives reset (flight semantics)                          | deferred    |

New tests added in `isc-fs/IFS08-CE-AMS#193` (numbered against the
v1.4.0 plan; old C-020..C-028 stay in place pending the renumber):

| Test  | What it checks                                                   | Status      |
|-------|------------------------------------------------------------------|-------------|
| C-036 | Transition: voltage drop during hold -> Error                    | implemented |
| C-039 | Run -> Error (sticky) on DASH_CHG drop (mirror of C-025)         | implemented |
| C-040 | Charge -> Error (sticky) on TSMS or DASH_CHG drop                | implemented |
| C-041 | `mode_locked` captured at Start->Precharge; killing heartbeat    | implemented |
|       |  mid-Run keeps mode = Car until VCU-stale predicate trips Error  |             |
| C-042 | `0x4A2[5]` cockpit byte encodes (sentinel, mode_locked, TSMS,    | implemented |
|       |  DASH_CHG) correctly across all 6 FSM states                     |             |

All TSMS/DASH_CHG-driven tests skip cleanly when either fixture is
unavailable (`tsms_*` / `dash_chg_*` keys absent from
`ams_profile.yaml` -- happens until the bench wires PF9/PF10 through
the TCA9555).
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_inputs(tsms, dash_chg):
    """Skip the test when either pin fixture is disabled."""
    missing = []
    if tsms     is None: missing.append("tsms_*")
    if dash_chg is None: missing.append("dash_chg_*")
    if missing:
        pytest.skip(f"Pin fixture(s) unavailable: {' + '.join(missing)} "
                    "keys absent from ams_profile.yaml. Fill in once "
                    "PF9/PF10 are wired through the TCA9555.")


def _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile):
    """Assert both inputs -> Start -> Precharge.  Returns Precharge snapshot."""
    tsms.assert_()
    dash_chg.assert_()
    return wait_for_state(
        M.FsmState.PRECHARGE,
        timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


def _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile):
    """Both inputs + ramped DC bus -> Run (car mode)."""
    _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)

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
# C-020 -- Start stays put with only one of the two inputs
# ---------------------------------------------------------------------------

class TestC020GateRequiresBoth:

    def test_c020_tsms_only(self, fresh_boot, tsms, dash_chg, wait_for_state,
                            observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        dash_chg.deassert()
        tsms.assert_()

        # Watch a full transition window; state must NOT leave Start.
        window_ms = int(ams_profile["state_transition_window_ms"]) + 100
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.START, (
                    f"FSM left Start with only TSMS asserted (now in "
                    f"{M.FsmState.name(state)}). Both TSMS and DASH_CHG "
                    f"must be high to fire the gate.")
            time.sleep(0.02)

    def test_c020_dash_chg_only(self, fresh_boot, tsms, dash_chg, wait_for_state,
                                observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        tsms.deassert()
        dash_chg.assert_()

        window_ms = int(ams_profile["state_transition_window_ms"]) + 100
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.START, (
                    f"FSM left Start with only DASH_CHG asserted (now in "
                    f"{M.FsmState.name(state)}).")
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# C-021 -- Start -> Precharge on TSMS && DASH_CHG
# ---------------------------------------------------------------------------

class TestC021StartToPrecharge:

    def test_c021(self, fresh_boot, tsms, dash_chg, wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        tsms.assert_()
        dash_chg.assert_()
        snap = wait_for_state(
            M.FsmState.PRECHARGE,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        assert snap["state"] == M.FsmState.PRECHARGE


# ---------------------------------------------------------------------------
# C-022 -- Precharge -> Transition on DC bus target
# ---------------------------------------------------------------------------

class TestC022PrechargeToTransition:

    def test_c022(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)

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

    def test_c023(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        run = _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                            ams_profile)
        assert run["state"] == M.FsmState.RUN


# ---------------------------------------------------------------------------
# C-024 -- Precharge timeout -> Error
# ---------------------------------------------------------------------------

class TestC024PrechargeTimeout:

    def test_c024(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)
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

class TestC025RunToErrorOnInputDrop:

    def test_c025_tsms_drop(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                            wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)

        tsms.deassert()
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


# ---------------------------------------------------------------------------
# C-026 -- Transition -> Charge in charger mode (VCU heartbeat paused)
# ---------------------------------------------------------------------------
# Car-vs-charger is captured at Start -> Precharge from VCU 0x100
# heartbeat freshness. Pause the heartbeat first, wait > kVcuFreshMs,
# then assert TSMS+DASH_CHG -> mode locks to Charger. After the lock
# fires the heartbeat can resume (or a oneshot bump) so precharge
# actually completes; the locked mode does NOT re-evaluate.

class TestC026TransitionToCharge:

    def test_c026(self, fresh_boot, tsms, dash_chg, acu_heartbeat, acu_stim,
                  wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)

        # Silence the VCU heartbeat for > kVcuFreshMs (1 s in firmware).
        acu_heartbeat["pause"]()
        time.sleep(1.2)

        # Assert both inputs -> Start->Precharge with mode_locked = Charger.
        tsms.assert_()
        dash_chg.assert_()
        wait_for_state(
            M.FsmState.PRECHARGE,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)

        # Mode already locked; safe to drive DC bus high so precharge
        # completes.
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
        # paths (current overlimit) need PF7 stim we don't have.
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


# ===========================================================================
# isc-fs/IFS08-CE-AMS#193 — new Block C tests for v1.4.0 acceptance.
# ===========================================================================


def _drive_to_charge(tsms, dash_chg, acu_heartbeat, acu_stim,
                     wait_for_state, ams_profile):
    """Start -> Precharge -> Transition -> Charge via paused-heartbeat
    path. Mirror of `_drive_to_run` for the charger mode-lock. Returns
    the Charge snapshot."""
    acu_heartbeat["pause"]()
    time.sleep(int(ams_profile.get("vcu_stale_ms", 1000)) / 1000.0 + 0.2)

    tsms.assert_()
    dash_chg.assert_()
    wait_for_state(M.FsmState.PRECHARGE,
                   timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)

    # Mode is now locked to Charger; push DC bus up to clear precharge.
    pack_V   = int(ams_profile["stub_expected_pack_mV"]) // 1000
    target_V = int(pack_V * 0.96)
    acu_stim.send_dc_bus_v(target_V)

    wait_for_state(M.FsmState.TRANSITION,
                   timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)

    hold_ms = int(ams_profile["transition_hold_ms"])
    return wait_for_state(M.FsmState.CHARGE,
                          timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# C-036 -- Transition voltage drop mid-hold -> Error
# ---------------------------------------------------------------------------
# DC bus reaches target, FSM enters Transition. During the
# `kTransitionHoldMs` hold window the heartbeat drops to 0 V; the
# transition predicate re-evaluates and the FSM should latch Error
# instead of advancing to Run.

class TestC036TransitionVoltageDrop:

    def test_c036(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)

        pack_V   = int(ams_profile["stub_expected_pack_mV"]) // 1000
        target_V = int(pack_V * 0.96)
        acu_heartbeat["set_volts"](target_V)
        wait_for_state(M.FsmState.TRANSITION,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)

        # Mid-hold: drop the bus before kTransitionHoldMs elapses. The
        # hold is short (default 100 ms) so we need to drop FAST.
        acu_heartbeat["set_volts"](0)

        # Expect Error, NOT Run. Generous slack: hold + transition
        # window + a couple of telemetry cycles.
        timeout_ms = (int(ams_profile["transition_hold_ms"]) +
                      int(ams_profile["state_transition_window_ms"]) +
                      2 * int(ams_profile["tx_telemetry_period_ms"]))
        wait_for_state(M.FsmState.ERROR, timeout_ms=timeout_ms)


# ---------------------------------------------------------------------------
# C-039 -- Run -> Error (sticky) on DASH_CHG drop
# ---------------------------------------------------------------------------
# Mirror of C-025 for the other operator input. PR #187 made every
# AIR-open event a sticky fault, so dropping DASH_CHG mid-Run must trip
# Error and stay there.

class TestC039RunToErrorOnDashChgDrop:

    def test_c039_dash_chg_drop(self, fresh_boot, tsms, dash_chg,
                                acu_heartbeat, wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)

        dash_chg.deassert()
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


# ---------------------------------------------------------------------------
# C-040 -- Charge -> Error (sticky) on TSMS or DASH_CHG drop
# ---------------------------------------------------------------------------
# Same conservative semantics in Charge state: any AIR-open event
# latches Error.

class TestC040ChargeToErrorOnInputDrop:

    def test_c040_tsms_drop(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                            acu_stim, wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_charge(tsms, dash_chg, acu_heartbeat, acu_stim,
                         wait_for_state, ams_profile)
        tsms.deassert()
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)

    def test_c040_dash_chg_drop(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                                acu_stim, wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_charge(tsms, dash_chg, acu_heartbeat, acu_stim,
                         wait_for_state, ams_profile)
        dash_chg.deassert()
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


# ---------------------------------------------------------------------------
# C-041 -- mode_locked captured exactly at Start -> Precharge
# ---------------------------------------------------------------------------
# Drive to Run via the car path (heartbeat fresh at Start->Precharge),
# then kill the heartbeat. The cockpit-byte `mode_locked` field stays
# at Car until the VCU-stale predicate eventually trips Error -- the
# mode lock does NOT re-evaluate just because the heartbeat went stale.

class TestC041ModeLockedCapturedAtPrecharge:

    def test_c041(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)

        # Read cockpit byte: expect mode_locked = Car at Run entry.
        ft = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert ft is not None, "no 0x4A2 frame at Run entry"
        cb = M.decode_telem_temps(ft.data)["cockpit"]
        assert cb["valid"] and cb["mode_locked"] == M.ModeLocked.CAR, (
            f"mode_locked != Car at Run entry: {cb}")

        # Kill the heartbeat. Sample the cockpit byte for slightly less
        # than `vcu_stale_ms`. mode_locked must stay = Car the whole
        # time. (We're proving the lock is sticky, not the staleness
        # predicate -- C-021 / B-021 already cover that.)
        acu_heartbeat["pause"]()
        vcu_stale_ms = int(ams_profile.get("vcu_stale_ms", 1000))
        deadline = time.monotonic() + (vcu_stale_ms - 200) / 1000.0
        samples = 0
        while time.monotonic() < deadline:
            ft = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
            if ft is not None:
                cb = M.decode_telem_temps(ft.data)["cockpit"]
                assert cb["valid"] and cb["mode_locked"] == M.ModeLocked.CAR, (
                    f"mode_locked re-evaluated mid-Run (heartbeat paused): "
                    f"{cb}. Lock is supposed to survive heartbeat loss "
                    f"until the staleness predicate trips Error.")
                samples += 1
            time.sleep(0.05)
        assert samples > 0, "no 0x4A2 frames seen while sampling"

        # After Error trips, mode_locked should still read Car (the
        # field doesn't reset on Error).
        post_grace_ms = vcu_stale_ms + int(ams_profile["tx_telemetry_period_ms"]) + 300
        wait_for_state(M.FsmState.ERROR, timeout_ms=post_grace_ms)
        ft = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert ft is not None
        cb = M.decode_telem_temps(ft.data)["cockpit"]
        assert cb["valid"] and cb["mode_locked"] == M.ModeLocked.CAR, (
            f"mode_locked lost after Error trip: {cb}")


# ---------------------------------------------------------------------------
# C-042 -- Cockpit byte encoding across all 6 FSM states
# ---------------------------------------------------------------------------
# Walk every state and verify `0x4A2[5]` bit-packs (sentinel,
# mode_locked, TSMS, DASH_CHG) per the encoder layout. Split into a
# car-mode path (Start -> Precharge -> Transition -> Run -> Error) and
# a charger-mode path (Start -> Precharge -> Transition -> Charge ->
# Error) -- can't reach both Run and Charge within a single boot
# because Error is sticky.

class TestC042CockpitByteAcrossStates:

    @staticmethod
    def _cb(observe_acu):
        ft = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert ft is not None, "no 0x4A2 frame"
        return M.decode_telem_temps(ft.data)["cockpit"]

    def test_c042_car_path(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                           wait_for_state, observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)

        # Start (fresh boot, pins still low)
        cb = self._cb(observe_acu)
        assert cb == {"valid": True, "raw": 0x80,
                      "mode_locked": M.ModeLocked.UNDECIDED,
                      "tsms": False, "dash_chg": False}, f"Start: {cb}"

        # Precharge (assert both inputs, heartbeat fresh -> mode = Car)
        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)
        cb = self._cb(observe_acu)
        assert (cb["valid"] and cb["mode_locked"] == M.ModeLocked.CAR
                and cb["tsms"] and cb["dash_chg"]), f"Precharge: {cb}"

        # Transition (drive DC bus up)
        pack_V   = int(ams_profile["stub_expected_pack_mV"]) // 1000
        acu_heartbeat["set_volts"](int(pack_V * 0.96))
        wait_for_state(M.FsmState.TRANSITION,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)
        cb = self._cb(observe_acu)
        assert (cb["valid"] and cb["mode_locked"] == M.ModeLocked.CAR
                and cb["tsms"] and cb["dash_chg"]), f"Transition: {cb}"

        # Run
        hold_ms = int(ams_profile["transition_hold_ms"])
        wait_for_state(M.FsmState.RUN,
                       timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)
        cb = self._cb(observe_acu)
        assert (cb["valid"] and cb["mode_locked"] == M.ModeLocked.CAR
                and cb["tsms"] and cb["dash_chg"]), f"Run: {cb}"

        # Error (drop TSMS to latch)
        tsms.deassert()
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        cb = self._cb(observe_acu)
        assert (cb["valid"] and cb["mode_locked"] == M.ModeLocked.CAR
                and not cb["tsms"] and cb["dash_chg"]), f"Error: {cb}"

    def test_c042_charger_path(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                               acu_stim, wait_for_state, observe_acu,
                               ams_profile):
        _require_inputs(tsms, dash_chg)

        # Start (same as car path; mode = Undecided)
        cb = self._cb(observe_acu)
        assert cb["raw"] == 0x80, f"Start: {cb}"

        # Pause heartbeat for > kVcuFreshMs so the Start->Precharge
        # transition locks Charger.
        acu_heartbeat["pause"]()
        vcu_stale_ms = int(ams_profile.get("vcu_stale_ms", 1000))
        time.sleep(vcu_stale_ms / 1000.0 + 0.2)

        # Precharge with mode = Charger
        tsms.assert_()
        dash_chg.assert_()
        wait_for_state(M.FsmState.PRECHARGE,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        cb = self._cb(observe_acu)
        assert (cb["valid"] and cb["mode_locked"] == M.ModeLocked.CHARGER
                and cb["tsms"] and cb["dash_chg"]), f"Precharge (charger): {cb}"

        # Push DC bus high via oneshot so precharge completes
        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        acu_stim.send_dc_bus_v(int(pack_V * 0.96))

        # Transition
        wait_for_state(M.FsmState.TRANSITION,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)
        cb = self._cb(observe_acu)
        assert (cb["valid"] and cb["mode_locked"] == M.ModeLocked.CHARGER
                and cb["tsms"] and cb["dash_chg"]), f"Transition (charger): {cb}"

        # Charge
        hold_ms = int(ams_profile["transition_hold_ms"])
        wait_for_state(M.FsmState.CHARGE,
                       timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)
        cb = self._cb(observe_acu)
        assert (cb["valid"] and cb["mode_locked"] == M.ModeLocked.CHARGER
                and cb["tsms"] and cb["dash_chg"]), f"Charge: {cb}"

        # Error from Charge (drop DASH_CHG)
        dash_chg.deassert()
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        cb = self._cb(observe_acu)
        assert (cb["valid"] and cb["mode_locked"] == M.ModeLocked.CHARGER
                and cb["tsms"] and not cb["dash_chg"]), f"Error (charger): {cb}"
