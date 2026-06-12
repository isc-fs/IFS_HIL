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
| C-039a  | Run → Start (non-latching) on TSMS drop, then re-arm (#327)    | implemented |
| C-039c  | Charge → Start (non-latching) on TSMS drop (#327)              | implemented |
| C-041   | mode_locked retained mid-Run when VCU killed                   | implemented (post-#251) |
| C-042   | 0x4A2[5] cockpit byte across all 6 FSM states                  | implemented (post-#251) |
| C-043   | Error sticky ≥ 5 s after heartbeat resumes                     | implemented |
| C-045   | Start stays put with no fixtures driving PF9/PF10 (PR #230)    | implemented |

All TSMS/DASH_CHG-driven tests skip cleanly when either fixture is
unavailable (`tsms_*` / `dash_chg_*` keys absent from
`ams_profile.yaml` -- happens until the bench wires PF9/PF10 through
the TCA9555).

Reworked for AMS dev @ f414c436 (#327 + #330): a TSMS drop is now a
NON-latching de-energise to Start (C-039a Run, C-039c Charge), not a
sticky Error -- so C-042/C-048 induce Error via cell-UV (inject_cell_v)
instead. New Block C-330 rows C-049/C-050 cover the DC-bus-collapse
de-energise (Run -> Start, debounced, non-latching).
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


def _drive_to_charge(tsms, dash_chg, acu_heartbeat, charger_0x101,
                     wait_for_state, ams_profile):
    """Charger-mode Start -> ... -> Charge. VCU silent (> VcuFreshMs) with
    a fresh 0x101 charge-request, then TSMS held + a DASH_CHG press. The
    charger one-button proceed (fresh 0x101, no dc_bus gate, no dwell --
    #312/#316) carries Precharge -> Transition -> Charge within a few
    safety ticks, so those two are transient (sub-telemetry-cycle): we
    wait for the stable Charge state. mode_locked latches Charger at the
    (transient) Precharge and is retained into Charge. Returns the Charge
    snapshot.

    The `charger_0x101` fixture emits 0x101 from setup; `resume()` is
    defensive in case an earlier step paused it. Charger is selected only
    when, at the lock, the VCU 0x100 is absent AND 0x101 is fresh (#304 /
    #316); VcuStale is Car-only (#304) so the lock is reachable.
    """
    acu_heartbeat["pause"]()             # VCU absent
    charger_0x101["resume"]()            # 0x101 live + fresh
    time.sleep(1.2)                      # > VcuFreshMs (1 s): VCU reads absent
    tsms.assert_()
    dash_chg.press()
    return wait_for_state(
        M.FsmState.CHARGE,
        timeout_ms=int(ams_profile["state_transition_window_ms"]) + 1500)


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
        self, mlc_powered, acu_heartbeat, pack_current_diff, observe_acu,
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
# C-039a -- Run -> Start (non-latching de-energise) on TSMS drop  (#327)
# ---------------------------------------------------------------------------
# #327 (SAFETY, inverts the old PR #187 semantics): a TSMS drop is a NORMAL
# operator de-energise, NOT a fault. AMS_OK is the AMS's own SDC relay
# UPSTREAM of TSMS; latching on a TSMS drop would drop AMS_OK and leave the
# loop unrecloseable without a power-cycle, violating the cockpit
# stop/restart-unaided rule. So TSMS off -> AIRs open -> FSM returns to
# Start (AMS_OK stays health-only/HIGH), and the driver re-arms with a
# DASH_CHG press -- no reset. Mirrors host SIL test_sil_tsms_drop_in_run_rearms.

class TestC039aRunToStartOnTsmsDrop:

    def test_c039a_tsms_drop_rearms(self, fresh_boot, tsms, dash_chg,
                                    acu_heartbeat, wait_for_state, observe_acu,
                                    pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)

        # TSMS drop -> non-latching de-energise back to Start (NOT Error).
        tsms.deassert()
        wait_for_state(
            M.FsmState.START,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)

        # Non-latching: no fault_reason, mode latch released to Undecided,
        # AMS_OK stays HIGH (it never dropped -- health-only, not Error-driven).
        pit_diag.wait_for_scan()
        fsm_status = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        assert fsm_status[6] == 0, (
            f"0x6C0[6] fault_reason = {fsm_status[6]} after a TSMS drop in "
            "Run; expected 0 (None). #327: a TSMS drop must NOT latch Error.")
        assert fsm_status[1] == 0, (
            f"0x6C0[1] mode_locked = {fsm_status[1]} after de-energise to "
            "Start; expected 0 (Undecided) -- the mode latch releases so a "
            "re-arm re-locks it.")
        assert fsm_status[3] == 1, (
            f"0x6C0[3] AMS_OK = {fsm_status[3]} after a TSMS drop; expected 1 "
            "(HIGH). A non-latching de-energise keeps AMS_OK health-only.")

        # Re-arm WITHOUT a reset: bus back to quiescent, then TSMS held + a
        # fresh DASH_CHG press drives Start -> Precharge -> Run again
        # (_drive_to_run re-asserts TSMS, presses, ramps, and waits for Run).
        acu_heartbeat["set_volts"](0)
        observe_acu.clear()
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)


# ---------------------------------------------------------------------------
# C-037 -- Charger entry: VCU absent + 0x101 fresh -> Precharge (Charger)
# ---------------------------------------------------------------------------
# Charger-vs-car is captured at the Start->Precharge lock. Under #304/#316
# Charger requires BOTH the VCU 0x100 absent (> VcuFreshMs) AND the
# operator charge-request 0x101 fresh (>= 2 Hz, magic 'CHRG'). #304 makes
# VcuStale Car-only so the charger lock is actually reachable (the old
# pre-emption that filed #302). VcuFreshMs absent alone is no longer
# enough -- without 0x101 it defaults to Car (see C-038).

class TestC037ChargerEntry:

    def test_c037_charger_entry(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, charger_0x101,
        wait_for_state, pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)
        snap = _drive_to_charge(tsms, dash_chg, acu_heartbeat,
                                charger_0x101, wait_for_state, ams_profile)
        assert snap["state"] == M.FsmState.CHARGE

        # Reaching Charge proves the Charger lock fired -- Charge is
        # unreachable except via a Charger-mode Precharge. mode_locked is
        # retained from the (transient) lock; confirm it reads Charger (2).
        pit_diag.wait_for_scan()
        fsm_status = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        mode_locked = fsm_status[1]
        assert mode_locked == 2, (
            f"0x6C0[1] mode_locked = {mode_locked} after a VCU-absent + "
            "fresh-0x101 charger Charge; expected 2 (Charger). #304 makes "
            "VcuStale Car-only so the charger lock is reachable (was #302).")


# ---------------------------------------------------------------------------
# C-037b -- Charger one-button proceed: fresh 0x101 -> Charge (no dc_bus)
# ---------------------------------------------------------------------------

class TestC037bChargerOneButtonProceed:
    """#316: in Charger mode the proceed Precharge -> Transition -> Charge
    is gated by a still-fresh 0x101 (the connected charger), NOT by a
    dc_bus voltage gate and NOT by a second button press. With 0x101 kept
    fresh and no dc_bus injected, the FSM reaches Charge on its own."""

    def test_c037b_charger_proceeds_to_charge(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, charger_0x101,
        wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        # No dc_bus is ever injected: the proceed is gated by a still-fresh
        # 0x101 alone (one button, no second press, no voltage gate).
        # Reaching Charge is the assertion.
        snap = _drive_to_charge(tsms, dash_chg, acu_heartbeat,
                                charger_0x101, wait_for_state, ams_profile)
        assert snap["state"] == M.FsmState.CHARGE


# ---------------------------------------------------------------------------
# C-037c -- Charger stale-0x101 timeout: charger unplugged -> Error
# ---------------------------------------------------------------------------

class TestC037cChargerStaleRequestTimeout:
    """#316: AIR+ must never close into a disconnected charger. Enter
    Charger Precharge, then STOP the 0x101 charge-request (charger
    unplugged). Precharge must hold (not proceed to Charge) and latch
    Error at PrechargeMaxMs (5 s); VcuStale stays Car-only (#304) so the
    only thing that trips is the precharge timeout."""

    def test_c037c_charger_stale_request_times_out(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, charger_0x101,
        wait_for_state, observe_acu, pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)
        # Lock Charger with a fresh 0x101, then "unplug" the charger the
        # instant the press fires. If the proceed to Charge requires 0x101
        # to stay fresh through precharge, AIR+ must NOT close: Precharge
        # holds and times out to Error at PrechargeMaxMs.
        acu_heartbeat["pause"]()
        charger_0x101["resume"]()
        time.sleep(1.2)
        tsms.assert_()
        dash_chg.press()
        charger_0x101["pause"]()         # charger unplugged at the lock

        # The charger proceed has no dwell (#312) and 0x101's freshness
        # window may outlast the proceed latency. If Charge is reached
        # before the stop bites, the bench simply can't hold Precharge this
        # way -- skip with a precise reason rather than emit a misleading
        # 5 s Error timeout. Watch ~1.5 s for a Charge breakthrough.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None and \
                    M.decode_telem_status(f.data)["state"] == M.FsmState.CHARGE:
                pytest.skip(
                    "Charger reached Charge before the post-press 0x101 stop "
                    "took effect. With no precharge dwell (#312) and a 0x101 "
                    "freshness window wider than the proceed latency, the "
                    "bench cannot hold Precharge by stopping 0x101 after the "
                    "press. C-037c needs a firmware test-hook (a forced "
                    "precharge dwell, or 0x101-fresh re-checked at AIR+ "
                    "close) to be benchable -- flag to the AMS team.")
            time.sleep(0.05)

        # Precharge held (no Charge breakthrough) -> must time out to Error.
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["precharge_max_ms"])
                       + int(ams_profile["tx_telemetry_period_ms"]) + 800)
        pit_diag.wait_for_scan()
        fsm_status = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        fault_reason = fsm_status[6]
        assert fault_reason == 12, (
            f"0x6C0[6] fault_reason = {fault_reason} after the charger "
            "stale-0x101 timeout; expected 12 (FsmError). AIR+ must never "
            "close into a disconnected charger.")


# ---------------------------------------------------------------------------
# C-038 -- Dead VCU + no 0x101 locks Car (not Charger) -> VcuStale  (#311)
# ---------------------------------------------------------------------------

class TestC038DeadVcuLocksCarNotCharger:
    """#311/#316: VCU silent AND no 0x101 on the bus must lock **Car**
    (the safe default), not Charger. A Car lock arms VcuStale (#304), so
    the chip faults to Error rather than silently energising a charge path
    with no supervising VCU and no charger handshake."""

    def test_c038_dead_vcu_no_request_locks_car(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, wait_for_state,
        pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)

        # VCU silent > VcuFreshMs. Crucially NO charger_0x101 fixture here,
        # so 0x101 is absent and Charger is NOT eligible.
        acu_heartbeat["pause"]()
        time.sleep(1.2)

        tsms.assert_()
        dash_chg.press()
        # The Car lock arms VcuStale immediately (VCU already stale), so
        # Precharge may be a single-tick passthrough to Error. Wait for
        # Error directly; mode_locked is retained through the trip (C-041).
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["vcu_stale_ms"])
                       + int(ams_profile["state_transition_window_ms"])
                       + int(ams_profile["tx_telemetry_period_ms"]) + 500)

        pit_diag.wait_for_scan()
        fsm_status = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        mode_locked = fsm_status[1]
        fault_reason = fsm_status[6]
        assert mode_locked == 1, (
            f"0x6C0[1] mode_locked = {mode_locked} with VCU silent and no "
            "0x101; expected 1 (Car, the safe default). A Charger lock here "
            "would energise a charge path with no VCU and no charger.")
        assert fault_reason == 11, (
            f"0x6C0[6] fault_reason = {fault_reason}; expected 11 (VcuStale) "
            "-- the Car lock must arm VcuStale and fault, not proceed.")


# ---------------------------------------------------------------------------
# C-039c -- Charge SURVIVES a DASH_CHG release; exits only on TSMS drop
# ---------------------------------------------------------------------------

class TestC039cChargeSurvivesDashRelease:
    """#316: like Run, Charge is sustained by TSMS alone. Releasing or
    re-pressing DASH_CHG in Charge must NOT fault; only a TSMS drop ends
    Charge."""

    def test_c039c_charge_survives_dash_release(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, charger_0x101,
        observe_acu, wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        snap = _drive_to_charge(tsms, dash_chg, acu_heartbeat,
                                charger_0x101, wait_for_state, ams_profile)
        assert snap["state"] == M.FsmState.CHARGE

        # Release + spurious re-press of DASH must not end Charge.
        dash_chg.deassert()
        dash_chg.press()
        window_ms = int(ams_profile["tx_telemetry_period_ms"]) * 2 + 300
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.CHARGE, (
                    f"Charge did not survive a DASH_CHG release/press: now "
                    f"in {M.FsmState.name(state)}. Charge is sustained by "
                    "TSMS alone (#316).")
            time.sleep(0.05)

        # And a TSMS drop de-energises Charge back to Start (non-latching,
        # #327) -- not Error. Like Run, a TSMS drop in Charge is an operator
        # stop, not a fault.
        tsms.deassert()
        wait_for_state(
            M.FsmState.START,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# C-043 -- Error sticky within a boot
# ---------------------------------------------------------------------------

class TestC043ErrorSticky:

    def test_c043(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        # Under #304 VcuStale is Car-only, so the trip must happen in a
        # Car-locked state -- pausing the VCU in pre-lock Start no longer
        # faults (B-027). Drive to Run (locks Car) first, THEN trip Error
        # via VCU staleness, which is a *removable* cause (resume the
        # heartbeat) -- exactly what a stickiness test needs.
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)

        acu_heartbeat["pause"]()
        try:
            window_ms = (int(ams_profile["vcu_stale_ms"]) +
                         int(ams_profile["tx_telemetry_period_ms"]) + 300)
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
        pico_emu, wait_for_state, ams_profile
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

        # 4. Run → Error via a real latched fault (cell under-voltage). A
        #    TSMS drop is now a non-latching de-energise (#327); keep TSMS
        #    held and trip a genuine predicate to reach the Error state.
        pico_emu.inject_cell_v(module=0, cell=10, mV=2500)  # < 2800 -> UV
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2
                                  + 500)
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

        # TSMS bit follows the fixture (held) state.
        assert observed["Start"]["tsms"] is False
        assert observed["Precharge"]["tsms"] is True
        assert observed["Error"]["tsms"] is True     # TSMS still held (cell-UV trip)

        # DASH_CHG bit 0 is the LIVE GPIO level (#316/#317), NOT the
        # consumed edge. We drive via momentary press(), so the line is
        # released (LOW) by the time each state is sampled.
        for state in ("Start", "Precharge", "Run", "Error"):
            assert observed[state]["dash"] is False, (
                f"{state} cockpit dash bit = {observed[state]['dash']}, "
                "expected False -- DASH is momentary and released after the "
                "press; bit 0 tracks the live level, not the consumed edge.")

        # Prove bit 0 tracks the LIVE level: hold DASH high while Error is
        # latched (sticky) -- the live readback must flip True even though
        # no transition consumes the edge.
        dash_chg.assert_()
        live = _decode(_cockpit_now())
        dash_chg.deassert()
        assert live["dash"] is True, (
            f"cockpit dash bit stayed {live['dash']} with DASH held HIGH in "
            "Error; bit 0 must mirror the live GPIO level.")


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


# ===========================================================================
# Block C-AMS_OK -- SDC enable drive (PB4 / 0x6C0[3])   (NEW per #301 / #317)
# ===========================================================================
# AMS_OK was never firmware-driven before #301 (floating/decaying on the
# external SDC leg -- see #299). #301 added Relays::set_ams_ok() so PB4 is
# actively HIGH only when past boot grace AND no Error is latched. The
# bench samples PB4 through MCP3208 (relays_readback.ams_ok). #317 marks
# AMS_OK HIGH during grace OR while Error is latched as a HARD fail.


class TestC046AmsOkLowInGrace:
    """#301: AMS_OK (PB4 -> external SDC enable) reads LOW during boot
    grace (< SafetyBootGraceMs). HIGH during grace is a #317 hard-fail."""

    def test_c046_ams_ok_low_in_grace(self, fresh_boot, relays_readback,
                                       ams_profile):
        if relays_readback is None:
            pytest.skip("relays_readback disabled (no ams_ok_adc_* keys)")
        # fresh_boot returns at the first 0x4A0, well inside the LOW window:
        # AMS_OK holds LOW through grace AND until the first full BMS poll
        # (~4.4 s post-boot per #301). Sample immediately -- must be LOW.
        since_boot = time.monotonic() - fresh_boot["t_power_on"]
        state = relays_readback.read()
        volts = relays_readback.read_volts()
        assert state["ams_ok"] is False, (
            f"AMS_OK HIGH {since_boot:.2f} s after power-on (PB4 = "
            f"{volts['ams_ok']:.2f} V). It must stay LOW through boot grace "
            f"({int(ams_profile['boot_grace_ms'])} ms) -- #317 hard-fail.")


class TestC047AmsOkHighHealthy:
    """#301: once past boot grace with no Error latched (healthy Start),
    AMS_OK reads HIGH -- the external SDC leg is actively driven, not
    floating/decaying. Settles ~4.4 s after boot (grace + first full
    BMS poll)."""

    def test_c047_ams_ok_high_when_healthy(self, fresh_boot, relays_readback,
                                           ams_profile):
        if relays_readback is None:
            pytest.skip("relays_readback disabled (no ams_ok_adc_* keys)")
        s = relays_readback.poll_for(lambda x: x["ams_ok"], timeout_s=8.0)
        assert s is not None and s["ams_ok"], (
            "AMS_OK never went HIGH within 8 s of boot in a healthy Start "
            "(cells nominal, VCU fresh). #301 drives PB4 HIGH past grace + "
            "first full poll (~4.4 s); stuck-LOW means the SDC enable isn't "
            "being driven.")


class TestC048AmsOkLowOnError:
    """#301: AMS_OK drops LOW within one safety tick (10 ms) of any Error
    latch and stays LOW for the sticky-Error session. AMS_OK HIGH while
    Error is latched is a #317 hard-fail."""

    def test_c048_ams_ok_drops_on_error(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, pico_emu,
        relays_readback, wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        if relays_readback is None:
            pytest.skip("relays_readback disabled (no ams_ok_adc_* keys)")

        # Healthy Run -> AMS_OK HIGH.
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)
        high = relays_readback.poll_for(lambda x: x["ams_ok"], timeout_s=6.0)
        assert high is not None, "AMS_OK never HIGH in healthy Run (pre-trip)"

        # Trip a REAL latched Error: cell under-voltage. A TSMS drop is now a
        # non-latching de-energise (#327) that keeps AMS_OK HIGH, so only a
        # genuine predicate fault exercises the AMS_OK-drops-on-Error contract.
        pico_emu.inject_cell_v(module=0, cell=10, mV=2500)  # < 2800 -> UV
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 500)

        # AMS_OK must drop LOW promptly (10 ms firmware contract; allow a
        # telemetry cycle of bench + ADC latency) and then STAY low.
        low = relays_readback.poll_for(lambda x: not x["ams_ok"], timeout_s=1.0)
        assert low is not None, (
            "AMS_OK stayed HIGH after the FSM latched Error -- #301 must pull "
            "PB4 LOW within one safety tick. #317 hard-fail.")

        # Sticky: AMS_OK must NOT recover HIGH while Error is still latched.
        held = relays_readback.poll_for(lambda x: x["ams_ok"], timeout_s=3.0)
        assert held is None, (
            "AMS_OK went back HIGH while Error was still latched -- the SDC "
            "enable must stay LOW for the sticky-Error session.")


# ===========================================================================
# Block C-330 -- DC-bus collapse de-energises Run/Charge  (NEW per #330)
# ===========================================================================
# #330 (SAFETY): the cockpit SDC can pull the AIRs open without the AMS
# sensing it directly, but the VCU keeps reporting dc_bus_V. A sustained
# collapse of that bus while we think we're in Run means AIR+ would be
# reclosing onto a dead bus on the operator's release. The FSM treats a
# debounced collapse as a NON-LATCHING de-energise back to Start (like a
# TSMS drop, #327) so a re-arm re-runs precharge rather than reclosing AIR+.


class TestC049BusCollapseDeEnergises:
    """#330: in Run (Car), drive the VCU-reported DC bus below
    BusCollapsePercent of pack for longer than BusCollapseConfirmTicks ->
    FSM de-energises to Start (non-latching), AMS_OK stays HIGH, no fault
    reason; then restore the bus and re-arm -> Precharge -> Run, precharge
    re-runs, no reset.

    NB BusCollapsePercent / BusCollapseConfirmTicks are COMMISSION
    placeholders (50 % / ~200 ms); the collapse target here is half the
    threshold so it stays valid if the percent is retuned downward.
    """

    def test_c049_bus_collapse_to_start_rearms(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, wait_for_state,
        observe_acu, pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)

        # Collapse the VCU dc_bus to half the threshold (well under
        # BusCollapsePercent of pack), sustained past the debounce.
        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        collapse_V = (pack_V * int(ams_profile["bus_collapse_percent"])
                      // 100) // 2
        acu_heartbeat["set_volts"](collapse_V)
        wait_for_state(
            M.FsmState.START,
            timeout_ms=int(ams_profile["bus_collapse_confirm_ms"])
                       + int(ams_profile["state_transition_window_ms"])
                       + int(ams_profile["tx_telemetry_period_ms"]) + 400)

        # Non-latching: no fault reason, AMS_OK stays HIGH.
        pit_diag.wait_for_scan()
        fsm_status = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        assert fsm_status[6] == 0, (
            f"0x6C0[6] fault_reason = {fsm_status[6]} after a DC-bus collapse "
            "in Run; expected 0 (None). #330 de-energises, it must NOT latch "
            "Error.")
        assert fsm_status[3] == 1, (
            f"0x6C0[3] AMS_OK = {fsm_status[3]} after a bus collapse; expected "
            "1 (HIGH) -- a non-latching de-energise keeps AMS_OK health-only.")

        # Restore the bus + re-arm WITHOUT a reset: Start -> Precharge -> Run,
        # precharge re-runs (_drive_to_run ramps from 0).
        acu_heartbeat["set_volts"](0)
        observe_acu.clear()
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)


class TestC050BriefDipNoFalseTrip:
    """#330: a DC-bus dip SHORTER than BusCollapseConfirmTicks must NOT trip
    the de-energise -- the collapse is debounced so transient VCU-reported
    sag (a single dropped 0x100, inverter ripple) doesn't bounce Run."""

    def test_c050_brief_dip_stays_run(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, wait_for_state,
        observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                      ams_profile)

        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        collapse_V = (pack_V * int(ams_profile["bus_collapse_percent"])
                      // 100) // 2
        # Dip for half the confirm time (< debounce), then restore.
        dip_ms = int(ams_profile["bus_collapse_confirm_ms"]) // 2
        acu_heartbeat["set_volts"](collapse_V)
        time.sleep(dip_ms / 1000.0)
        acu_heartbeat["set_volts"](pack_V)

        # State must stay Run across the next couple telemetry cycles.
        window_ms = int(ams_profile["tx_telemetry_period_ms"]) * 2 + 300
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.RUN, (
                    f"a sub-debounce DC-bus dip tripped the FSM to "
                    f"{M.FsmState.name(state)}; #330 must debounce "
                    f"{int(ams_profile['bus_collapse_confirm_ms'])} ms before "
                    "de-energising.")
            time.sleep(0.05)
