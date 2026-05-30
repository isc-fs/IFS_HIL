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

Per `isc-fs/IFS08-CE-AMS#272` (supersedes #245 — dropped C-036, C-040,
C-044 as scaffolding-gap or flight-build-only):

| #272 ID | What it checks                                                 | Status      |
|---------|----------------------------------------------------------------|-------------|
| C-030   | Start stays put with only TSMS                                 | implemented |
| C-031   | Start stays put with only DASH_CHG                             | implemented |
| C-032   | Start → Precharge on TSMS && DASH_CHG (mode locks Car)         | implemented |
| C-033   | Precharge → Transition once DC bus hits 95 % of pack           | implemented |
| C-037   | Charger mode FSM: VCU paused → Start→Precharge→Transition→Charge | implemented |
| C-038   | Run → Error (sticky) on TSMS drop                              | implemented |
| C-039   | Run → Error (sticky) on DASH_CHG drop                          | implemented |
| C-041   | mode_locked retained mid-Run when VCU killed                   | implemented (post-#251) |
| C-042   | 0x4A2[5] cockpit byte across all 6 FSM states                  | implemented (post-#251) |
| C-043   | Error sticky ≥ 5 s after heartbeat resumes                     | implemented |
| C-045   | Start stays put with no fixtures driving PF9/PF10 (PR #230)    | implemented |

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
    """TSMS held + a DASH_CHG press -> Start -> Precharge (#316 edge model).
    Returns the Precharge snapshot."""
    tsms.assert_()          # held master switch
    dash_chg.press()        # momentary rising edge fires the transition
    return wait_for_state(
        M.FsmState.PRECHARGE,
        timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


def _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile):
    """Both inputs + RC-ramped DC bus -> Run (car mode).

    Per AMS PR #244 the Transition state is a single-tick passthrough
    (Precharge → Transition → Run within one 10 ms safety tick), so the
    20 ms test poll window can't reliably observe Transition. We wait
    for Run directly — it's the stable post-Transition state. The
    intermediate Transition is still emitted on 0x4A0 but only briefly;
    catch-or-not-catch is not what the test is asserting.

    The bus voltage is *ramped* (not stepped). Real precharge rises
    along an RC curve through the 690 Ω resistor; stepping 0 → ~pack
    in one tick trips an AMS Error from Precharge before Transition
    ever fires.
    """
    _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)

    # AMS predicate (state_machine.hpp::precharge_target_reached):
    #   bus_mV * 100 >= pack_mV * 95   (i.e. bus >= 95 % of pack)
    # `stub_expected_pack_mV` is our nominal — the actual pack reported
    # by the LTC chain can flicker ±a few V (discovery in progress, PEC
    # retries on chip-1). Targeting 96 % leaves a sub-1 % margin and
    # we sometimes fall below the threshold, fire `!target_reached` in
    # Transition, and land in Error. Target the full pack to keep the
    # comparison comfortably true through the noise.
    pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
    acu_heartbeat["ramp_to"](pack_V)

    hold_ms = int(ams_profile["transition_hold_ms"])
    return wait_for_state(
        M.FsmState.RUN,
        timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# C-020 -- Start stays put with only one of the two inputs
# ---------------------------------------------------------------------------

class TestC030C031GateRequiresBoth:

    def test_c030_tsms_only(self, fresh_boot, tsms, dash_chg, wait_for_state,
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
                    f"FSM left Start with only TSMS held, no DASH_CHG press "
                    f"(now in {M.FsmState.name(state)}). The gate needs TSMS "
                    f"held AND a DASH_CHG rising edge.")
            time.sleep(0.02)

    def test_c031_dash_chg_only(self, fresh_boot, tsms, dash_chg, wait_for_state,
                                observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        tsms.deassert()
        dash_chg.press()       # a press with no TSMS held must fire nothing

        window_ms = int(ams_profile["state_transition_window_ms"]) + 100
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.START, (
                    f"FSM left Start with a DASH_CHG press but no TSMS held "
                    f"(now in {M.FsmState.name(state)}).")
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# C-021 -- Start -> Precharge on TSMS && DASH_CHG
# ---------------------------------------------------------------------------

class TestC032StartToPrecharge:

    def test_c032(self, fresh_boot, tsms, dash_chg, wait_for_state,
                  pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        tsms.assert_()
        dash_chg.press()       # momentary press (rising edge), not a held level
        snap = wait_for_state(
            M.FsmState.PRECHARGE,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        assert snap["state"] == M.FsmState.PRECHARGE

        # Per AMS #272 C-032: mode_locked latches Car on the
        # Start→Precharge edge. Observe via pit-diag 0x6C0[1] which
        # mirrors g_mode_locked_telemetry: 0=Undecided, 1=Car, 2=Charger.
        # `wait_for_scan()` blocks until a complete pit-diag burst lands
        # AFTER the transition (anchored on 0x6C6) — otherwise we'd risk
        # reading the cached 0x6C0 from the pre-transition scan (up to
        # 1 s old, mode_locked still 0).
        pit_diag.wait_for_scan()
        fsm_status = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        mode_locked = fsm_status[1]
        assert mode_locked == 1, (
            f"0x6C0[1] mode_locked = {mode_locked} after Start→Precharge; "
            "expected 1 (Car). VCU 0x100 was fresh during the transition "
            "so the latch should have caught Car mode.")


# ---------------------------------------------------------------------------
# C-032b -- a DASH_CHG level held from before boot fires no edge  (#316)
# ---------------------------------------------------------------------------

class TestC032bHeldLineNoFire:
    """#316: SafetyTask seeds its DASH_CHG edge-detector from the *live*
    level at init. A line held HIGH continuously from before boot
    therefore presents no low->high edge and must NOT trigger
    Start->Precharge, even with TSMS held. Only a fresh release->press
    edge fires the gate.

    This needs a boot with DASH_CHG already HIGH -- the opposite of
    `fresh_boot`, which deasserts both cockpit pins before power-on. So
    the power-cycle is driven by hand here, holding both lines high
    across the relay close.
    """

    def test_c032b_held_dash_does_not_fire(
        self, mlc_powered, acu_heartbeat, current_heartbeat, observe_acu,
        tsms, dash_chg, wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        import os
        from broker.server import BrokerClient
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        relay_bit = mlc_powered["relay_bit"]
        try:
            # Power off, then assert BOTH cockpit lines HIGH *before* the
            # relay closes so the app boots seeing DASH already high.
            client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit,
                        value=False)
            time.sleep(2.0)
            tsms.assert_()
            dash_chg.assert_()        # held HIGH through boot -> no edge
            observe_acu.clear()
            client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit,
                        value=True)
        finally:
            client.close()

        # App must boot to Start and STAY there: the held DASH presents no
        # edge. Watch through boot grace + a couple of telemetry cycles.
        first = wait_for_state(M.FsmState.START, timeout_ms=5000)
        assert first["state"] == M.FsmState.START

        window_ms = (int(ams_profile["boot_grace_ms"])
                     + int(ams_profile["tx_telemetry_period_ms"]) * 2 + 500)
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.START, (
                    f"FSM left Start to {M.FsmState.name(state)} with "
                    "DASH_CHG held HIGH from boot. A held level must fire no "
                    "edge (#316); only a release->press does.")
            time.sleep(0.05)

        # Prove the gate still works: a genuine release->press edge (TSMS
        # still held) must now fire Start -> Precharge.
        dash_chg.press()
        wait_for_state(
            M.FsmState.PRECHARGE,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


# ---------------------------------------------------------------------------
# C-022 -- Precharge -> Transition on DC bus target
# ---------------------------------------------------------------------------

class TestC033PrechargeToTransition:
    """Per AMS PR #244 the Transition state is a single-tick passthrough
    (Precharge → Transition → Run within one 10 ms safety tick). The
    20 ms test poll window can't reliably observe Transition. Waiting
    for Run is the right success signal for the Precharge → Transition
    contract — reaching Run proves the gate fired (Run is unreachable
    from Precharge without going through Transition)."""

    def test_c033(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)

        # Ramp to full pack (not 96 %): the predicate is `bus >= 95 % of
        # pack`, and at 96 % we don't have enough margin against pack
        # jitter — see `_drive_to_run` for the full reasoning.
        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        acu_heartbeat["ramp_to"](pack_V)

        wait_for_state(
            M.FsmState.RUN,
            timeout_ms=int(ams_profile["state_transition_window_ms"])
                       + int(ams_profile["transition_hold_ms"])
                       + 100)


# ---------------------------------------------------------------------------
# C-034 -- Car precharge timeout -> Error (FsmError)   (#307, re-added)
# ---------------------------------------------------------------------------
# History: PrechargeMaxMs was deleted in #244 (Precharge held
# indefinitely), then RE-ADDED in #307 as a 5 s safety timeout. #309's
# *time-based dwell* was reverted in #312 -- a timeout (give up -> Error)
# is not a dwell (minimum hold), so this row is valid and the revert
# doesn't touch it. C-035 (old TransitionHold) stays gone: Transition is
# still a single-tick passthrough (#244).

class TestC034CarPrechargeTimeout:
    """#307: in Car mode, if the injected DC bus never reaches the 95 %
    target, Precharge must not hold the precharge resistor closed
    forever -- it latches Error at PrechargeMaxMs (5 s) with
    `0x6C0[6]` == 12 (FsmError)."""

    def test_c034_car_precharge_timeout(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, wait_for_state,
        pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)

        # Lock Car and enter Precharge, but leave the DC bus at the
        # quiescent 0 V (acu_heartbeat default) -- precharge_target_reached
        # never fires. The heartbeat keeps emitting (VCU fresh), so the
        # only failing predicate is the precharge timeout, not VcuStale.
        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)

        # Hold ~PrechargeMaxMs + a telemetry cycle; Error must latch.
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["precharge_max_ms"])
                       + int(ams_profile["tx_telemetry_period_ms"]) + 800)

        # Fault reason must be FsmError (12) -- the precharge-timeout path,
        # not a stale/predicate trip beating it to the latch.
        pit_diag.wait_for_scan()
        fsm_status = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        fault_reason = fsm_status[6]
        assert fault_reason == 12, (
            f"0x6C0[6] fault_reason = {fault_reason} after the Car precharge "
            "timeout; expected 12 (FsmError). A different reason means a "
            "predicate (VcuStale/current) tripped before the 5 s timeout.")

# ---------------------------------------------------------------------------
# C-039a -- Run -> Error (latched) on TSMS drop
# ---------------------------------------------------------------------------
# Operator chose conservative semantics in PR #187: every AIR-open event
# is a sticky fault requiring power-cycle. Run / Charge no longer have
# a "clean shutdown back to Start" path. TSMS (the held master switch) is
# the ONLY thing that ends Run -- releasing DASH_CHG does not (C-039b).

class TestC039aRunToErrorOnTsmsDrop:

    def test_c039a_tsms_drop(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
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

class TestC037ChargerModeFsm:

    def test_c037(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, pit_diag, ams_profile):
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

        # Per AMS #272 C-037: with VCU stale at the Start→Precharge
        # edge, mode_locked must latch Charger (= 2) not Car. Read via
        # pit-diag 0x6C0[1] — wait for a fresh scan so we don't get
        # the pre-transition cached frame (same staleness fix as C-032).
        pit_diag.wait_for_scan()
        fsm_status = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        mode_locked = fsm_status[1]
        assert mode_locked == 2, (
            f"0x6C0[1] mode_locked = {mode_locked} after Charger-mode "
            "Start→Precharge; expected 2 (Charger). VCU was stale "
            f"(paused 1.2 s > VcuFreshMs {ams_profile.get('vcu_fresh_ms', 1000)} ms) "
            "so the latch should have caught Charger mode.")

        # Mode already locked; safe to drive DC bus high so precharge
        # completes. Transition is a single-tick passthrough (#244) so
        # the 20 ms test poll can't catch it; wait for Charge directly
        # (Charger mode's analog of Run; both are unreachable from
        # Precharge without going through Transition).
        #
        # Resume heartbeat + ramp bus to full pack. mode_locked is
        # already latched to Charger, so the resumed heartbeat won't
        # re-evaluate Car. Target = pack (not 96 %) so we have margin
        # against pack jitter — see `_drive_to_run` for reasoning.
        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        acu_heartbeat["resume"]()
        acu_heartbeat["ramp_to"](pack_V)

        # Hold elapses -> Charge (not Run, because mode_locked = Charger).
        hold_ms = int(ams_profile["transition_hold_ms"])
        wait_for_state(
            M.FsmState.CHARGE,
            timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# C-027 -- Error sticky within a boot
# ---------------------------------------------------------------------------

class TestC043ErrorSticky:

    def test_c043(self, fresh_boot, acu_heartbeat, wait_for_state,
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

# C-044 (Error survives reset) dropped per AMS #272 — flight-build
# only and HIL_CLEAR explicitly wipes the latch on every boot by design.
#
# C-036 (Transition voltage drop) dropped per AMS #272 — needs a dc_bus
# step-down stim that the acu_heartbeat fixture doesn't support. File
# under "scaffolding gap" if the stim path ever lands.

# ---------------------------------------------------------------------------
# C-039b — Run SURVIVES a DASH_CHG release  (#316 — REPLACES old C-039)
# ---------------------------------------------------------------------------
# The old C-039 ("Run → Error on DASH_CHG drop") is INVERTED under #316.
# DASH_CHG is a momentary press: by the time Run is reached the line is
# already released, and Run is sustained by TSMS alone. Releasing or
# re-pressing DASH_CHG in Run must NOT fault. (#317 warns a strict-xfail
# on the old row would xpass for the wrong reason — hence a real,
# inverted assertion here.)

class TestC039bRunSurvivesDashRelease:

    def test_c039b_run_survives_dash_release(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, observe_acu,
        wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        run = _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                            ams_profile)
        assert run["state"] == M.FsmState.RUN

        # DASH is already low after the press that drove Start->Precharge.
        # Explicitly release, then fire a fresh spurious press: neither is
        # an exit condition in Run. State must stay Run across ~2 telemetry
        # cycles (TSMS stays held throughout).
        dash_chg.deassert()
        dash_chg.press()
        window_ms = int(ams_profile["tx_telemetry_period_ms"]) * 2 + 300
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.RUN, (
                    f"Run did not survive a DASH_CHG release/press: now in "
                    f"{M.FsmState.name(state)}. Under #316 Run is sustained "
                    "by TSMS alone; DASH_CHG edges must be ignored in Run.")
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# C-041 — mode_locked retained mid-Run when VCU killed  (AMS #272 row)
# ---------------------------------------------------------------------------

class TestC041ModeLockedRetained:
    """Per #245: the mode latch (Car / Charger) is captured at
    Start→Precharge and held for the rest of the FSM lifetime. Killing
    the VCU mid-Run must NOT flip mode — it should trip VCU-stale and
    land in Error with mode_locked == Car still readable.

    Observable via the cockpit byte in 0x4A2[5]: bits 2..3 encode the
    mode latch (0=Undecided, 1=Car, 2=Charger). Unskipped post-#251
    which hoisted the encoding out of HIL_STUB.
    """

    def test_c041_mode_locked_retained_through_error(
        self, fresh_boot, observe_acu, tsms, dash_chg, acu_heartbeat,
        wait_for_state, ams_profile
    ):
        _require_inputs(tsms, dash_chg)

        # 1. Drive Start → Precharge → Run; mode latches Car on the
        #    Start→Precharge edge.
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)

        # Sample cockpit byte while in Run. Bits 2..3 should be 0b01 (Car).
        f_run = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert f_run is not None, "no 0x4A2 frame in Run"
        mode_run = (f_run.data[5] >> 2) & 0x03
        assert mode_run == 1, (
            f"mode_locked in Run = {mode_run}, expected 1 (Car). "
            f"Full cockpit byte = 0x{f_run.data[5]:02X}")

        # 2. Kill VCU heartbeat and wait past VcuStaleMs → FSM trips Error.
        acu_heartbeat["pause"]()
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["vcu_stale_ms"])
                                  + int(ams_profile["tx_telemetry_period_ms"])
                                  + 300)

        # 3. mode_locked must STILL read Car after the transition to Error.
        f_err = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert f_err is not None, "no 0x4A2 frame after entering Error"
        mode_err = (f_err.data[5] >> 2) & 0x03
        assert mode_err == 1, (
            f"mode_locked after Error = {mode_err}, expected 1 (Car) -- "
            "mode latch was supposed to survive the predicate trip. "
            f"Full cockpit byte = 0x{f_err.data[5]:02X}")


# ---------------------------------------------------------------------------
# C-042 — 0x4A2[5] cockpit byte across all 6 FSM states  (NEW per #245)
# ---------------------------------------------------------------------------

class TestC042CockpitByteAcrossStates:
    """Per #245: the cockpit byte at `0x4A2[5]` encodes the FSM-visible
    cockpit state across every state. Bit layout (verified against
    safety_task.cpp post-#251):

        bit 7   sentinel — set whenever firmware is running
        bits 4..6 reserved (0)
        bits 2..3 mode_locked (0 Undecided / 1 Car / 2 Charger)
        bit 1   TSMS GPIO readback
        bit 0   DASH_CHG GPIO readback

    Unskipped post-#251 (cockpit byte hoist out of HIL_STUB).
    """

    def test_c042_cockpit_byte_per_state(
        self, fresh_boot, observe_acu, tsms, dash_chg, acu_heartbeat,
        wait_for_state, ams_profile
    ):
        _require_inputs(tsms, dash_chg)

        def _cockpit_now():
            """Wait one telemetry cycle + slack, return 0x4A2[5]."""
            time.sleep(int(ams_profile["tx_telemetry_period_ms"]) / 1000.0
                       + 0.2)
            f = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
            assert f is not None, "no 0x4A2 after state settle"
            return f.data[5]

        def _decode(cockpit_byte):
            return {
                "sentinel": bool(cockpit_byte & 0x80),
                "mode":     (cockpit_byte >> 2) & 0x03,
                "tsms":     bool(cockpit_byte & 0x02),
                "dash":     bool(cockpit_byte & 0x01),
            }

        # 1. Start (no fixtures driving) — already in fresh_boot.
        observed = {}
        observed["Start"] = _decode(_cockpit_now())

        # 2. Drive Start → Precharge (mode latches Car).
        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)
        observed["Precharge"] = _decode(_cockpit_now())

        # 3. Drive Precharge → Transition → Run (ramp to full pack;
        #    a step jump, or 96 % target, can trip the predicate).
        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        acu_heartbeat["ramp_to"](pack_V)
        wait_for_state(M.FsmState.RUN,
                       timeout_ms=int(ams_profile["state_transition_window_ms"])
                                  + int(ams_profile["transition_hold_ms"])
                                  + 200)
        observed["Run"] = _decode(_cockpit_now())

        # 4. Run → Error via TSMS drop.
        tsms.deassert()
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["state_transition_window_ms"])
                                  + 200)
        observed["Error"] = _decode(_cockpit_now())

        # Assertions: sentinel always set; mode_locked = Car (1) once
        # latched and through Error.
        for state, c in observed.items():
            assert c["sentinel"], f"sentinel bit clear in state {state}: {c}"

        assert observed["Start"]["mode"] == 0, (
            f"Start should be mode=Undecided (0), got {observed['Start']}")
        for state in ("Precharge", "Run", "Error"):
            assert observed[state]["mode"] == 1, (
                f"{state} should retain mode=Car (1), got {observed[state]}")

        # TSMS bit follows the fixture state.
        assert observed["Start"]["tsms"] is False
        assert observed["Precharge"]["tsms"] is True
        assert observed["Error"]["tsms"] is False    # we dropped it


# ---------------------------------------------------------------------------
# C-045 — Start stays put with no fixtures driving PF9/PF10  (NEW per #245)
# ---------------------------------------------------------------------------

class TestC045StartStaysWithNoCockpitInputs:
    """Per #245 (post-#230): with no HIL fixtures driving PF9/PF10,
    the AMS firmware's internal GPIO_PULLDOWN keeps TSMS + DASH_CHG
    LOW. FSM must stay in Start indefinitely (or until VCU-stale
    grace expires) and NEVER spuriously transition to Precharge.

    This is the regression net for AMS PR #230 — the pre-#230
    GPIO_NOPULL config let the inputs float to mid-rail, which the
    FSM occasionally read as HIGH and triggered phantom Precharge
    transitions in early bench runs.

    Method: power-cycle the chip without ever calling tsms.assert_()
    or dash_chg.assert_(). The `fresh_boot` fixture already deasserts
    both pins pre-power-on (#37's cockpit-pre-deassert fix), so this
    is just a "watch state for a few cycles" test.
    """

    def test_c045_no_inputs_no_transition(self, fresh_boot, observe_acu,
                                            wait_for_state, ams_profile):
        # First frame from fresh_boot should be Start.
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START, (
            "first frame post-boot wasn't Start; fresh_boot's cockpit "
            "pre-deassert may have regressed."
        )
        # Watch for ~2 telemetry cycles. Within that window state must
        # stay Start. (Allow Error as a soft-pass: VCU staleness CAN
        # trip if the heartbeat dropped a frame -- that's a separate
        # predicate; what we're checking here is that we DON'T see
        # Precharge / Transition / Run / Charge.)
        deadline = time.monotonic() + \
            (int(ams_profile["tx_telemetry_period_ms"]) * 2 / 1000.0) + 0.3
        spurious_transitions: list[str] = []
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                d = M.decode_telem_status(f.data)
                st = d["state"]
                if st not in (M.FsmState.START, M.FsmState.ERROR):
                    spurious_transitions.append(d["state_name"])
                    break
            time.sleep(0.05)
        assert not spurious_transitions, (
            f"FSM transitioned to {spurious_transitions[0]} with no "
            "cockpit inputs driven. PR #230's GPIO_PULLDOWN contract "
            "may have regressed: PF9 / PF10 are floating to HIGH and "
            "the firmware is reading them as asserted."
        )
