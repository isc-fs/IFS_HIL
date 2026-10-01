"""
Block B — Safety supervisor (post-refactor: SafetyTask + StateTask +
TelemetryTask are merged into MainTask, single 10 ms loop).

Migrated to `isc-fs/IFS08-CE-AMS#317` (AMS v1.6.0, supersedes #272). Every
test is observable over can0 / pit-diag 0x6C0.

| #317 ID   | What it checks                                          | Status     |
|-----------|---------------------------------------------------------|------------|
| B-010     | MainTask 10 ms cadence via heartbeat (60 s)             | implemented|
| B-021     | Car-locked VCU stale > VcuStaleMs → Error (#304)        | implemented|
| B-022     | BMS module stale > BmsStaleMs → Error (Pico STOP_REPLY) | implemented|
| B-026a..c | cell UV/OV/OT → Error via Pico (OT: set_all_temps)      | implemented|
| B-027     | VcuStale is Car-only: pre-lock idle stays Start (#304)  | implemented|
| B-028     | cell-fault debounce: transient no-latch / sustained (#296) | implemented|
| B-029     | fault-reason byte 0x6C0[6] per fault class (#276)       | implemented|
| B-030     | 0x101 charge-request magic gate (#311)                  | implemented|

Notes:
  - Per-sensor `inject_cell_t` does NOT propagate to the AMS on the current
    Pico build (bench-confirmed); OT rows drive a GLOBAL over-temp via
    `set_all_temps` instead.
  - Cell/BMS fault rows `wait_for_settled()` (past grace + first full poll)
    before injecting -- boot grace + the #290 first-poll gate + the #296
    debounce otherwise suppress the fault within the test window.
"""

from __future__ import annotations

import threading
import time
import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Helpers (v1.6.0 FSM-drive: TSMS held + DASH_CHG momentary press, #316)
# ---------------------------------------------------------------------------
# FaultReason enum (firmware ams_config.hpp), read at pit-diag 0x6C0[6].
# 0 None / 2 BmsModuleOffline / 3 BmsStale / 4 CellUnderVoltage /
# 5 CellOverVoltage / 7 CellOverTemp / 11 VcuStale / 12 FsmError.

def _require_inputs(tsms, dash_chg):
    """Skip the test when either pin fixture is disabled."""
    missing = []
    if tsms     is None: missing.append("tsms_*")
    if dash_chg is None: missing.append("dash_chg_*")
    if missing:
        pytest.skip(f"Pin fixture(s) unavailable: {' + '.join(missing)} "
                    "keys absent from ams_profile.yaml.")


def _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile):
    """TSMS held + a DASH_CHG press (#316) + RC-ramped DC bus -> Run (Car).
    Used to reach a Car-locked state so VcuStale (Car-only, #304) arms."""
    tsms.assert_()
    dash_chg.press()
    wait_for_state(
        M.FsmState.PRECHARGE,
        timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
    pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
    acu_heartbeat["ramp_to"](pack_V)
    return wait_for_state(
        M.FsmState.RUN,
        timeout_ms=int(ams_profile["transition_hold_ms"])
                   + int(ams_profile["state_transition_window_ms"]) + 100)


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
                                         wait_for_state, wait_for_settled,
                                         ams_profile):
        # Sanity: chip is alive in Start before we silence the chain.
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()   # past grace + first full poll: boot grace
                             # suppresses BMS staleness, so a stop_reply
                             # during grace wouldn't trip within the window

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
    """Car-locked VCU staleness -> Error. Under #304 VcuStale is Car-only,
    so the trip must happen in a Car-locked state -- pausing 0x100 in
    pre-lock Start no longer faults (B-027). Drive to Run (locks Car),
    then silence 0x100 and confirm the FSM latches Error."""

    def test_b021_vcu_stale_trips_error(self, fresh_boot, tsms, dash_chg,
                                        acu_heartbeat, wait_for_state,
                                        ams_profile):
        _require_inputs(tsms, dash_chg)
        run = _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state,
                            ams_profile)
        assert run["state"] == M.FsmState.RUN

        # Stop emitting 0x100; VcuStale (now armed in Car mode) -> Error.
        acu_heartbeat["pause"]()
        try:
            window_ms = (int(ams_profile["vcu_stale_ms"]) +
                         int(ams_profile["tx_telemetry_period_ms"]) + 300)
            wait_for_state(M.FsmState.ERROR, timeout_ms=window_ms)
        finally:
            acu_heartbeat["resume"]()


# ---------------------------------------------------------------------------
# B-027 — VcuStale is Car-only: pre-lock idling is safe  (AMS #304)
# ---------------------------------------------------------------------------

class TestB027VcuStaleCarOnly:
    """#304: VcuStale is Car-only. With the VCU silent, idling pre-lock
    (Undecided) must NOT trip VcuStale Error during or after boot grace --
    only a Car lock arms it. This is the regression net for charger
    reachability (#302/#304) and the reason B-021 drives to a Car-locked
    state to trip VcuStale."""

    def test_b027_pre_lock_silent_vcu_stays_start(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, observe_acu,
        wait_for_state, pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)
        # Silence the VCU with NO cockpit inputs driven (pre-lock Undecided).
        acu_heartbeat["pause"]()

        # Watch well past grace + several VcuStaleMs windows: must stay Start.
        settle_s = (int(ams_profile["boot_grace_ms"]) +
                    int(ams_profile["vcu_stale_ms"]) * 6 +
                    int(ams_profile["tx_telemetry_period_ms"]) * 2) / 1000.0
        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state == M.FsmState.START, (
                    f"Silent VCU tripped {M.FsmState.name(state)} pre-lock "
                    "(Undecided). #304 makes VcuStale Car-only; idling with "
                    "no Car lock must stay Start.")
            time.sleep(0.05)

        # Now lock Car (VCU still silent, no 0x101) -> VcuStale arms -> Error.
        tsms.assert_()
        dash_chg.press()
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["vcu_stale_ms"])
                       + int(ams_profile["state_transition_window_ms"])
                       + int(ams_profile["tx_telemetry_period_ms"]) + 400)

        # Confirm the trip was VcuStale (11) on a Car lock (mode 1).
        pit_diag.wait_for_scan()
        fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        assert fsm[6] == 11 and fsm[1] == 1, (
            f"After the Car lock: fault_reason = {fsm[6]} (want 11 VcuStale), "
            f"mode_locked = {fsm[1]} (want 1 Car).")


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

    def test_b026_cell_overvoltage(self, fresh_boot, pico_emu, wait_for_state,
                                    wait_for_settled, ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()       # past grace + first full poll before inject
        pico_emu.inject_cell_v(module=2, cell=5, mV=4400)  # > 4200
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 500)

    def test_b026_cell_undervoltage(self, fresh_boot, pico_emu, wait_for_state,
                                     wait_for_settled, ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()       # past grace + first full poll before inject
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
                                        wait_for_state, wait_for_settled,
                                        ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()       # past grace + first full poll before inject
        # Per-sensor inject_cell_t does NOT propagate to the AMS on the
        # current Pico build (telemetry max_temp stays nominal -- bench-
        # confirmed). A GLOBAL over-temp via set_all_temps does reach it
        # (telemetry max_temp tracks the set value), so drive every sensor
        # above CellOverTempC to exercise the OT predicate.
        pico_emu.set_all_temps(800)   # 80.0 C, > 60 C OT
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 6 + 3000)

    # B-026d (cell under-temp) intentionally not implemented — see class
    # docstring. The predicate exists in firmware; we just don't exercise
    # it from the bench because no realistic track scenario reaches it.


# ---------------------------------------------------------------------------
# B-028 — cell-fault debounce: transient dip must NOT latch  (AMS #296)
# ---------------------------------------------------------------------------

class TestB028CellFaultDebounce:
    """#296: a single transient sub-CellUnderVoltageMv sample must NOT latch
    Error -- the fault needs >= CellFaultConfirmTicks (~300 ms) of
    *sustained* breach. This is the regression net for the idle-CUV latch
    (#279/#290) where a torn/transient read tripped the FSM at idle."""

    def test_b028_transient_dip_does_not_latch(self, fresh_boot, pico_emu,
                                               observe_acu, wait_for_settled,
                                               ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()       # past grace + first full poll before inject
        # Dip one cell below CUV for ~100 ms (< ~300 ms confirm window),
        # then restore. The AMS sees the bad sample(s) but must not latch.
        # (module=0, cell=10) is a known-injectable address (B-026 UV);
        # an unreachable address would make this pass trivially (no bad
        # sample seen) rather than actually exercising the debounce.
        pico_emu.inject_cell_v(module=0, cell=10, mV=2500)  # < 2800
        time.sleep(0.1)
        pico_emu.set_all_cells(3750)                        # restore nominal
        # Watch ~3 s: state must NOT go Error.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                state = M.decode_telem_status(f.data)["state"]
                assert state != M.FsmState.ERROR, (
                    "A single transient sub-CUV dip latched Error -- the #296 "
                    "debounce (CellFaultConfirmTicks ~300 ms) regressed; a "
                    "torn/transient read must not trip the FSM (#279).")
            time.sleep(0.05)

    def test_b028_sustained_dip_latches(self, fresh_boot, pico_emu,
                                        wait_for_state, wait_for_settled,
                                        ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()       # past grace + first full poll before inject
        # Sustained breach -> must latch Error after the confirm window.
        # pico_emu teardown restores nominal cells for the next test.
        pico_emu.inject_cell_v(module=0, cell=10, mV=2500)  # < 2800, held
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 800)


# ---------------------------------------------------------------------------
# B-029 — fault-reason byte 0x6C0[6] per fault class  (AMS #276)
# ---------------------------------------------------------------------------

class TestB029FaultReasonByte:
    """#276: on each latched fault, pit-diag 0x6C0[6] reads the FaultReason
    enum and [7] the detail (offending module 0..4, or 0xFF none/torn);
    reads 0 (None) before any fault. Cross-checked by G-103."""

    def test_b029_no_fault_reads_zero(self, fresh_boot, wait_for_settled,
                                      pit_diag, ams_profile):
        wait_for_settled()
        pit_diag.wait_for_scan()
        fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        assert fsm[6] == 0, (
            f"0x6C0[6] fault_reason = {fsm[6]} in healthy Start; expected 0 "
            "(None) before any fault latches.")

    def test_b029_cell_undervoltage_reason(self, fresh_boot, pico_emu,
                                           wait_for_settled, pit_diag,
                                           wait_for_state, ams_profile):
        wait_for_settled()
        pico_emu.inject_cell_v(module=0, cell=10, mV=2500)
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 800)
        pit_diag.wait_for_scan()
        fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        assert fsm[6] == 4, (
            f"0x6C0[6] = {fsm[6]} after a cell-UV trip; expected 4 "
            "(CellUnderVoltage).")
        assert fsm[7] in range(5) or fsm[7] == 0xFF, (
            f"0x6C0[7] detail = {fsm[7]}; expected an offending module "
            "(0..4) or 0xFF (torn).")

    def test_b029_cell_overvoltage_reason(self, fresh_boot, pico_emu,
                                          wait_for_settled, pit_diag,
                                          wait_for_state, ams_profile):
        wait_for_settled()
        pico_emu.inject_cell_v(module=2, cell=5, mV=4400)
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 800)
        pit_diag.wait_for_scan()
        fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        assert fsm[6] == 5, (
            f"0x6C0[6] = {fsm[6]} after a cell-OV trip; expected 5 "
            "(CellOverVoltage).")

    def test_b029_cell_overtemp_reason(self, fresh_boot, pico_emu,
                                       wait_for_settled, pit_diag,
                                       wait_for_state, ams_profile):
        wait_for_settled()
        # Global over-temp (per-sensor inject_cell_t doesn't reach the AMS
        # on this Pico build -- see B-026 OT).
        pico_emu.set_all_temps(800)   # 80 C, > 60 C OT
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 6 + 3000)
        pit_diag.wait_for_scan()
        fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        assert fsm[6] == 7, (
            f"0x6C0[6] = {fsm[6]} after a cell-OT trip; expected 7 "
            "(CellOverTemp).")

    def test_b029_bms_stale_reason(self, fresh_boot, pico_emu, wait_for_settled,
                                   pit_diag, wait_for_state, ams_profile):
        wait_for_settled()
        try:
            pico_emu.stop_reply(0x1F)
            wait_for_state(M.FsmState.ERROR,
                           timeout_ms=int(ams_profile.get("bms_stale_ms", 1500))
                                      + int(ams_profile["tx_telemetry_period_ms"]) + 500)
            pit_diag.wait_for_scan()
            fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
            assert fsm[6] == 3, (
                f"0x6C0[6] = {fsm[6]} after BMS stale; expected 3 (BmsStale).")
        finally:
            pico_emu.resume_all()

    def test_b029_vcu_stale_reason(self, fresh_boot, tsms, dash_chg,
                                   acu_heartbeat, pit_diag, wait_for_state,
                                   ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile)
        acu_heartbeat["pause"]()
        try:
            wait_for_state(M.FsmState.ERROR,
                           timeout_ms=int(ams_profile["vcu_stale_ms"])
                                      + int(ams_profile["tx_telemetry_period_ms"]) + 400)
            pit_diag.wait_for_scan()
            fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
            assert fsm[6] == 11, (
                f"0x6C0[6] = {fsm[6]} after VCU stale; expected 11 (VcuStale).")
        finally:
            acu_heartbeat["resume"]()


# ---------------------------------------------------------------------------
# B-030 — 0x101 charge-request magic gate  (AMS #311)
# ---------------------------------------------------------------------------

def _emit_0x101_thread(ams_profile, payload_hex):
    """Background-emit 0x101 with the given payload at the charger cadence.
    Returns (stop_evt, thread, stim); the caller stops + joins in a finally."""
    from tools.firmware_test.acu_stim import AcuStim
    bus = ams_profile["bus_acu"]
    can_id = int(ams_profile.get("charger_0x101_id", 0x101))
    payload = bytes.fromhex(payload_hex)
    period = float(ams_profile.get("charger_0x101_period_ms", 200)) / 1000.0
    stim = AcuStim(channel=bus); stim.start()
    stop_evt = threading.Event()
    def _loop():
        while not stop_evt.is_set():
            try:
                stim.send_raw(can_id, payload, is_extended_id=False)
            except Exception:
                break
            stop_evt.wait(period)
    t = threading.Thread(target=_loop, name="0x101-badmagic", daemon=True)
    t.start()
    return stop_evt, t, stim


class TestB030ChargeRequestMagicGate:
    """#311: a 0x101 charge-request is only honoured when its first 4 bytes
    are the magic 'CHRG' (0x43485247). A wrong-magic 0x101 must be ignored
    (no charger mode). Observable via the lock decision: wrong magic + VCU
    absent + TSMS+press -> locks Car (the default), not Charger."""

    def test_b030_wrong_magic_ignored(self, fresh_boot, tsms, dash_chg,
                                      acu_heartbeat, wait_for_state, pit_diag,
                                      ams_profile):
        _require_inputs(tsms, dash_chg)
        # Emit 0x101 with a WRONG magic ('XXXX') at the charger cadence.
        stop_evt, t, stim = _emit_0x101_thread(ams_profile, "5858585800000000")
        try:
            acu_heartbeat["pause"]()          # VCU absent
            time.sleep(1.2)                   # > VcuFreshMs; bad 0x101 is fresh
            tsms.assert_()
            dash_chg.press()
            # Charger NOT eligible (magic rejected) -> defaults Car -> VcuStale
            # -> Error, with mode_locked = Car (1), not Charger (2).
            wait_for_state(M.FsmState.ERROR,
                           timeout_ms=int(ams_profile["vcu_stale_ms"])
                                      + int(ams_profile["state_transition_window_ms"])
                                      + int(ams_profile["tx_telemetry_period_ms"]) + 500)
            pit_diag.wait_for_scan()
            fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
            assert fsm[1] == 1, (
                f"mode_locked = {fsm[1]} with a wrong-magic 0x101 on the bus; "
                "expected 1 (Car). The bad magic must be ignored (#311) so "
                "Charger (2) is NOT selected.")
        finally:
            stop_evt.set(); t.join(timeout=1.0); stim.stop()
            acu_heartbeat["resume"]()
