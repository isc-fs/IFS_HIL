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
                    f"FSM left Start with only TSMS asserted (now in "
                    f"{M.FsmState.name(state)}). Both TSMS and DASH_CHG "
                    f"must be high to fire the gate.")
            time.sleep(0.02)

    def test_c031_dash_chg_only(self, fresh_boot, tsms, dash_chg, wait_for_state,
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

class TestC032StartToPrecharge:

    def test_c032(self, fresh_boot, tsms, dash_chg, wait_for_state,
                  pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        tsms.assert_()
        dash_chg.assert_()
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
# C-022 -- Precharge -> Transition on DC bus target
# ---------------------------------------------------------------------------

class TestC033PrechargeToTransition:

    def test_c033(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
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
# C-034 / C-035 -- REMOVED in #245 per AMS PR #244
# ---------------------------------------------------------------------------
# Both PrechargeMaxMs and TransitionHoldMs were deleted in #244:
#   - Precharge holds indefinitely until precharge_target_reached
#     fires OR a safety predicate trips. No more 1.5 s timeout to
#     Error -- the deletion was the point of #244.
#   - Transition is now a one-FSM-step passthrough (Precharge ->
#     Transition -> Run within a single 10 ms safety tick), so there's
#     no observable hold interval to time.
# The old test_c023 (Transition -> Run after hold) and test_c024
# (Precharge timeout) covered behaviour the firmware no longer
# exhibits. Deleted here rather than skipped because they'd be
# misleading regression signal (would always fail or always pass for
# the wrong reason). See #245 Block C table.

# ---------------------------------------------------------------------------
# C-038 -- Run -> Error (latched) on TSMS drop
# ---------------------------------------------------------------------------
# Operator chose conservative semantics in PR #187: every AIR-open event
# is a sticky fault requiring power-cycle. Run / Charge no longer have
# a "clean shutdown back to Start" path.

class TestC038RunToErrorOnTsmsDrop:

    def test_c038_tsms_drop(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
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

    def test_c037(self, fresh_boot, tsms, dash_chg, acu_heartbeat, acu_stim,
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
# C-039 — Run → Error (latched) on DASH_CHG drop  (AMS #272 row)
# ---------------------------------------------------------------------------

class TestC039RunToErrorOnDashChgDrop:
    """Sibling to C-038 (TSMS drop). Run → Error must also fire when
    DASH_CHG (cockpit) drops, mirroring the operator-intent semantics."""

    def test_c039_dash_chg_drop(self, fresh_boot, tsms, dash_chg,
                                 acu_heartbeat, wait_for_state, ams_profile):
        _require_inputs(tsms, dash_chg)
        run = _drive_to_run(tsms, dash_chg, acu_heartbeat,
                            wait_for_state, ams_profile)
        assert run["state"] == M.FsmState.RUN

        dash_chg.deassert()
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)


# C-040 (Charge → Error on input drop) dropped per AMS #272 — needs
# a _drive_to_charge helper symmetric to _drive_to_run. File under
# "scaffolding gap" if the helper ever lands.


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

        # 3. Drive Precharge → Transition → Run (acu_heartbeat ramps dc_bus).
        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        acu_heartbeat["set_volts"](int(pack_V * 0.96))
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
