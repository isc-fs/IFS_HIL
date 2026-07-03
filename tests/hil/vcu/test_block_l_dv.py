"""
Block L (DV drive mode) — the uDV autonomous contract + the dual-trigger R2D FSM
+ the 0x507 DV torque path (IFS08-CE-ECU #101, branch feat/dv-mode).

Extends the #94 A-J suite. The uDV output frames (0x504 ts-active, 0x505 brake-over,
0x506 mechanical rpm, 0x511 r2d-confirm) are UNGATED on the ACU bus (can2) -- they
stream from boot, no pit-diag enable needed. The inputs the uDV drives are 0x507
(torque cmd, s32 LE integer %, stale >100 ms) and 0x510 (R2D request, stale >200 ms).

DV R2D is a DUAL trigger at WaitStartBrake: manual (start button + brake > BrakeArmRaw)
OR DV (0x510 fresh + brake > BrakeDvHardRaw=2500); manual takes precedence and does NOT
latch DV. dv_latched_ persists until any drive-cycle exit (-> Precharge) or AmsError.

Image: feat/dv-mode, stubs off + ECU_HIL_STUB_START_BTN (bench PB5) for the manual cases.
Drive-path cases need a live DAC. L-015 (manual suite unaffected) = re-run #94 D + G
(test_block_c_fsm + test_block_g_plausibility) on this image with the DV inputs silent.
"""
from __future__ import annotations

import time

import pytest

from tools.firmware_test.vcu import can_map as M
from tools.firmware_test.vcu.can_map import InvMode
from tools.firmware_test.vcu.can_map import VcuFsmState as S


def _status(observe_acu, timeout_s=1.5):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_PIT_STATUS, extended=False)
        if f is not None and len(f.data) >= 8:
            return M.decode_pit_status(f.data)
        time.sleep(0.02)
    return None


def _fsm(observe_acu):
    s = _status(observe_acu, 0.6)
    return s["fsm_state"] if s is not None else None


def _wait_fsm(observe_acu, pred, timeout_s):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        s = _status(observe_acu, 0.3)
        if s is not None:
            last = s["fsm_state"]
            if pred(last):
                return last
        time.sleep(0.03)
    return last


def _last(observe, can_id, decoder, timeout_s=1.5):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        f = observe.last(can_id, extended=False)
        if f is not None:
            return decoder(f.data)
        time.sleep(0.02)
    return None


def _r2d_confirm(observe_acu):
    d = _last(observe_acu, M.ID_DV_R2D_CONFIRM, M.decode_r2d_confirm)
    return d["r2d_confirm"] if d else None


def _torque_nm(observe_inv):
    d = _last(observe_inv, M.ID_INV_TORQUE, M.decode_inv_torque)
    return d["torque_nm"] if d else None


def _apps_raw(vcu_profile, which, pct):
    lo = int(vcu_profile[f"{which}_adc_min"])
    hi = int(vcu_profile[f"{which}_adc_max"])
    return int(lo + pct / 100.0 * (hi - lo))


def _send_once(bus, can_id, data):
    """One-shot CAN frame (for the 'send 0x510 ONCE' stale case)."""
    from tools.firmware_test.acu_stim import AcuStim
    s = AcuStim(channel=bus)
    s.start()
    try:
        s.send_raw(can_id, data, is_extended_id=False)
        time.sleep(0.05)
    finally:
        s.stop()


def _setup_wsb(inv_heartbeat, acu_inject, observe_acu, vcu_profile):
    """Setup-WSB: walk to WaitStartBrake (fsm=2) -- inverter Vdc + AMS precharge OK."""
    inv_heartbeat["vdc_ready"]()
    acu_inject["set_precharge"](1)
    return _wait_fsm(observe_acu, lambda s: s == S.WAIT_START_BRAKE, 6.0)


def _setup_dv_active(inv_heartbeat, acu_inject, udv_inject, pedals, observe_acu, vcu_profile):
    """Setup-DV-Active (L-005 walk): WSB -> DV R2D (0x510 + brake>2500, no start btn) ->
    R2dDelay -> WaitInvStandby -> Active, DV-latched. Releases the entry brake at the end
    (the latch persists); leaves 0x510 streaming. Returns the reached state (None on skip)."""
    if _setup_wsb(inv_heartbeat, acu_inject, observe_acu, vcu_profile) != S.WAIT_START_BRAKE:
        return None
    pedals["set_brake"](int(vcu_profile["brake_dv_hard_raw"]) + 400)   # > 2500
    udv_inject["set_r2d"](1)
    inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))    # ready for WaitInvStandby->Active
    active = _wait_fsm(observe_acu, lambda s: s == S.ACTIVE,
                       float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 6.0)
    pedals["set_brake"](0)   # release; dv_latched_ persists
    return active


# ============================ L-A: ECU->uDV streams =========================

class TestL001TsActive:
    def test_l001_ts_active_tracks_precharge(self, fresh_boot, acu_inject, observe_acu,
                                             vcu_profile):
        """L-001: 0x504 ts_active -- 0 with no AMS; 0x020 fresh -> 1; AMS stale >200ms -> 0."""
        time.sleep(0.3)
        d = _last(observe_acu, M.ID_DV_TS_ACTIVE, M.decode_ts_active)
        assert d is not None, "no 0x504 (should be ungated from boot)"
        assert d["ts_active"] == 0, "ts_active set with no AMS precharge"

        acu_inject["set_precharge"](1)
        time.sleep(0.4)
        assert _last(observe_acu, M.ID_DV_TS_ACTIVE, M.decode_ts_active)["ts_active"] == 1, \
            "ts_active not set with fresh 0x020"

        acu_inject["stop_all"]()          # silence 0x020 + 0x12C + 0x4A0 -> AMS stale
        time.sleep(0.4)                   # > AmsStaleMs (200)
        assert _last(observe_acu, M.ID_DV_TS_ACTIVE, M.decode_ts_active)["ts_active"] == 0, \
            "ts_active stayed 1 after AMS went stale"


class TestL002BrakeOverLimit:
    def test_l002_brake_over_limit(self, fresh_boot, pedals, observe_acu, vcu_profile):
        """L-002: 0x505 brake verdict -- brake < 2500 -> 0; > 2500 -> 1."""
        hard = int(vcu_profile["brake_dv_hard_raw"])
        pedals["set_brake"](hard - 500)
        time.sleep(0.3)
        assert _last(observe_acu, M.ID_DV_BRAKE_OVER, M.decode_brake_over)["brake_over_limit"] == 0
        pedals["set_brake"](hard + 500)
        time.sleep(0.3)
        assert _last(observe_acu, M.ID_DV_BRAKE_OVER, M.decode_brake_over)["brake_over_limit"] == 1, \
            "0x505 not set with brake > BrakeDvHardRaw"


class TestL003MotorRpm:
    @pytest.mark.parametrize("erpm,mech", [(0, 0), (32767, 3276), (-32767, -3276), (74560, 7456)])
    def test_l003_motor_rpm(self, fresh_boot, inv_heartbeat, observe_acu, erpm, mech):
        """L-003: 0x506 mechanical rpm = inverter erpm / 10 (pole pairs), s32 LE, sign kept."""
        inv_heartbeat["set_rpm"](erpm)
        time.sleep(0.4)
        d = _last(observe_acu, M.ID_DV_MOTOR_RPM, M.decode_motor_rpm)
        assert d is not None, "no 0x506"
        assert d["motor_rpm"] == mech, f"0x506 motor_rpm {d['motor_rpm']} != {mech} (erpm {erpm}/10)"


# ============================ L-B: the DV R2D gate ==========================

class TestL004DvR2dRefusedNoBrake:
    def test_l004_refusal_without_ebs_braking(self, fresh_boot, inv_heartbeat, acu_inject,
                                              udv_inject, pedals, pit_diag, observe_acu,
                                              vcu_profile):
        """L-004: 0x510 requesting but brake < 2500 -> fsm holds at 2, 0x511 stays 0."""
        if _setup_wsb(inv_heartbeat, acu_inject, observe_acu, vcu_profile) != S.WAIT_START_BRAKE:
            pytest.skip("never reached WaitStartBrake")
        pedals["set_brake"](int(vcu_profile["brake_dv_hard_raw"]) - 400)   # < 2500 (>900, but no start btn)
        udv_inject["set_r2d"](1)
        time.sleep(1.0)
        assert _fsm(observe_acu) == S.WAIT_START_BRAKE, "DV entered without hard braking"
        assert _r2d_confirm(observe_acu) == 0, "0x511 confirmed without hard braking"


class TestL005DvEntry:
    def test_l005_dv_entry(self, fresh_boot, inv_heartbeat, acu_inject, udv_inject, pedals,
                           pit_diag, observe_acu, vcu_profile):
        """L-005: 0x510 fresh + brake > 2500 -> R2dDelay (rtds_active) -> Active; 0x511=1
        from R2D onward; no start button."""
        if _setup_wsb(inv_heartbeat, acu_inject, observe_acu, vcu_profile) != S.WAIT_START_BRAKE:
            pytest.skip("never reached WaitStartBrake")
        pedals["set_brake"](int(vcu_profile["brake_dv_hard_raw"]) + 400)
        udv_inject["set_r2d"](1)
        r2d = _wait_fsm(observe_acu, lambda s: s == S.R2D_DELAY, 3.0)
        assert r2d == S.R2D_DELAY, f"did not enter R2dDelay (fsm {S.name_of(r2d)})"
        st = _status(observe_acu)
        assert st["flags"] & M.PIT_FLAG_RTDS_ACTIVE, "rtds_active not set in R2dDelay"
        assert _r2d_confirm(observe_acu) == 1, "0x511 not confirmed at R2D entry"

        inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
        active = _wait_fsm(observe_acu, lambda s: s == S.ACTIVE,
                           float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0)
        assert active == S.ACTIVE, f"did not reach Active (fsm {S.name_of(active)})"
        assert _r2d_confirm(observe_acu) == 1, "0x511 lost in Active"


class TestL006StaleDvRequest:
    def test_l006_stale_dv_request_refused(self, fresh_boot, inv_heartbeat, acu_inject,
                                           pedals, pit_diag, observe_acu, vcu_profile):
        """L-006: a single 0x510 (stale >200ms by the time brake > 2500) must NOT enter."""
        if _setup_wsb(inv_heartbeat, acu_inject, observe_acu, vcu_profile) != S.WAIT_START_BRAKE:
            pytest.skip("never reached WaitStartBrake")
        pedals["set_brake"](500)   # brake LOW while the single 0x510 is fresh -> no entry
        _send_once(vcu_profile["bus_acu"], int(vcu_profile["udv_r2d_req_id"]), M.encode_udv_r2d(1))
        time.sleep(0.4)            # 0x510 now stale (> UdvR2dStaleMs 200)
        pedals["set_brake"](int(vcu_profile["brake_dv_hard_raw"]) + 400)   # NOW > 2500, but 0x510 stale
        time.sleep(0.4)
        assert _fsm(observe_acu) == S.WAIT_START_BRAKE, "entered R2D on a stale 0x510"


class TestL009DualTriggerPrecedence:
    def test_l009_manual_precedence(self, fresh_boot, inv_heartbeat, acu_inject, udv_inject,
                                    pedals, start_button, pit_diag, observe_acu, vcu_profile):
        """L-009: start stub + brake > 2500 + 0x510 all present -> enters R2D but MANUAL:
        0x511 stays 0 (manual trigger has precedence, no DV latch)."""
        if _setup_wsb(inv_heartbeat, acu_inject, observe_acu, vcu_profile) != S.WAIT_START_BRAKE:
            pytest.skip("never reached WaitStartBrake")
        pedals["set_brake"](int(vcu_profile["brake_dv_hard_raw"]) + 400)   # satisfies both thresholds
        start_button["press"]()
        udv_inject["set_r2d"](1)
        r2d = _wait_fsm(observe_acu, lambda s: s >= S.R2D_DELAY, 3.0)
        assert r2d is not None and r2d >= S.R2D_DELAY, "did not enter R2D"
        time.sleep(0.3)
        assert _r2d_confirm(observe_acu) == 0, \
            "0x511 confirmed -- manual precedence not honored (latched as DV)"


# ============================ L-C: DV torque ===============================

class TestL007DvTorque:
    def test_l007_torque_from_0x507(self, fresh_boot, inv_heartbeat, acu_inject, udv_inject,
                                    pedals, pit_diag, observe_acu, observe_inv, vcu_profile):
        """L-007: at DV-Active, 0x507=40 -> 0x700.torque_pct=40 and 0x362 = -80 (ECU-negated),
        pedals untouched."""
        if _setup_dv_active(inv_heartbeat, acu_inject, udv_inject, pedals, observe_acu,
                            vcu_profile) != S.ACTIVE:
            pytest.skip("never reached DV-Active (DAC/gates)")
        udv_inject["set_torque"](40)
        time.sleep(0.5)
        assert _status(observe_acu)["torque_pct"] == 40, "0x700.torque_pct != 40"
        assert _torque_nm(observe_inv) == M.dv_torque_nm(40), \
            f"0x362 {_torque_nm(observe_inv)} != {M.dv_torque_nm(40)} (40% -> -80)"


class TestL008StaleTorqueNeverApps:
    def test_l008_stale_torque_zero_no_apps_fallback(self, fresh_boot, inv_heartbeat,
                                                     acu_inject, udv_inject, pedals, pit_diag,
                                                     observe_acu, observe_inv, vcu_profile):
        """L-008: stop 0x507 (>100ms) WHILE APPS DACs at ~50% -> 0x362 = 0, torque_pct = 0,
        fsm stays Active (no drive exit, NO pedal fallback); resume -> torque returns."""
        if _setup_dv_active(inv_heartbeat, acu_inject, udv_inject, pedals, observe_acu,
                            vcu_profile) != S.ACTIVE:
            pytest.skip("never reached DV-Active (DAC/gates)")
        udv_inject["set_torque"](40)
        time.sleep(0.5)
        assert _torque_nm(observe_inv) == M.dv_torque_nm(40), "no DV torque before the stale test"

        pedals["set_apps1"](_apps_raw(vcu_profile, "apps1", 50))
        pedals["set_apps2"](_apps_raw(vcu_profile, "apps2", 50))
        udv_inject["stop_torque"]()
        time.sleep(0.5)               # > UdvCmdStaleMs (100)
        assert _torque_nm(observe_inv) == 0, "0x362 non-zero -- stale DV torque or APPS fallback"
        assert _status(observe_acu)["torque_pct"] == 0, "0x700.torque_pct != 0 on stale DV"
        assert _fsm(observe_acu) == S.ACTIVE, "left Active on stale DV torque"

        udv_inject["set_torque"](40)
        time.sleep(0.5)
        assert _torque_nm(observe_inv) == M.dv_torque_nm(40), "DV torque did not resume"


class TestL010ConditionerFailsafes:
    def test_l010_conditioner_failsafes(self, fresh_boot, inv_heartbeat, acu_inject,
                                        udv_inject, pedals, pit_diag, observe_acu, observe_inv,
                                        vcu_profile):
        """L-010: 0x507 = -1 -> 0; 150 -> pct 100 (or cap) -> -240; 5 -> below the 10% floor -> 0."""
        if _setup_dv_active(inv_heartbeat, acu_inject, udv_inject, pedals, observe_acu,
                            vcu_profile) != S.ACTIVE:
            pytest.skip("never reached DV-Active (DAC/gates)")
        udv_inject["set_torque"](-1)
        time.sleep(0.5)
        assert _torque_nm(observe_inv) == 0, "negative 0x507 not clamped to 0"

        udv_inject["set_torque"](150)
        time.sleep(0.5)
        assert _status(observe_acu)["torque_pct"] == 100, "150 not clamped to 100"
        assert _torque_nm(observe_inv) == M.dv_torque_nm(100), "100% -> 0x362 != -240"

        udv_inject["set_torque"](5)
        time.sleep(0.5)
        assert _torque_nm(observe_inv) == 0, "5% not below the deadband floor"


class TestL011Ev23Exemption:
    def test_l011_ev23_exemption(self, fresh_boot, inv_heartbeat, acu_inject, udv_inject,
                                 pedals, pit_diag, observe_acu, observe_inv, vcu_profile):
        """L-011: at DV-Active with 0x507=50, EBS-style brake > 3000 must NOT cut DV torque:
        torque_pct stays 50, ev_2_3 bit stays 0 (pedals idle)."""
        if _setup_dv_active(inv_heartbeat, acu_inject, udv_inject, pedals, observe_acu,
                            vcu_profile) != S.ACTIVE:
            pytest.skip("never reached DV-Active (DAC/gates)")
        udv_inject["set_torque"](50)
        time.sleep(0.4)
        pedals["set_brake"](int(vcu_profile["brake_apps_raw"]) + 400)   # > 3000 (BrakePressedRaw)
        time.sleep(0.6)
        st = _status(observe_acu)
        assert st["torque_pct"] == 50, f"DV torque cut by brake (torque_pct {st['torque_pct']})"
        assert not (st["flags"] & M.PIT_FLAG_EV_2_3), "ev_2_3 latched in DV with pedals idle"
        assert _torque_nm(observe_inv) == M.dv_torque_nm(50), "0x362 not driving DV torque"


# ======================= L-D: exits & pre-emption ==========================

class TestL012CycleExitClearsLatch:
    def test_l012_cycle_exit_clears_latch(self, fresh_boot, inv_heartbeat, acu_inject,
                                          udv_inject, pedals, start_button, pit_diag,
                                          observe_acu, vcu_profile):
        """L-012: drive-cycle exit (drop 0x020 -> Precharge) clears the DV latch; a manual
        re-entry (start + brake 900<x<2500) re-enters R2D with 0x511=0 (mode re-decided)."""
        if _setup_dv_active(inv_heartbeat, acu_inject, udv_inject, pedals, observe_acu,
                            vcu_profile) != S.ACTIVE:
            pytest.skip("never reached DV-Active (DAC/gates)")
        assert _r2d_confirm(observe_acu) == 1, "not DV-latched at Active"

        acu_inject["set_precharge"](0)   # ok_precharge false -> Active -> Precharge
        if _wait_fsm(observe_acu, lambda s: s == S.PRECHARGE, 3.0) != S.PRECHARGE:
            pytest.skip("did not return to Precharge on precharge drop")
        assert _r2d_confirm(observe_acu) == 0, "DV latch not cleared on cycle exit"

        acu_inject["set_precharge"](1)
        udv_inject["stop_r2d"]()          # no DV request this cycle
        if _wait_fsm(observe_acu, lambda s: s == S.WAIT_START_BRAKE, 4.0) != S.WAIT_START_BRAKE:
            pytest.skip("did not re-reach WaitStartBrake")
        pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 300)   # 900 < x < 2500 = manual only
        start_button["press"]()
        r2d = _wait_fsm(observe_acu, lambda s: s >= S.R2D_DELAY, 3.0)
        assert r2d is not None and r2d >= S.R2D_DELAY, "manual re-entry failed"
        time.sleep(0.3)
        assert _r2d_confirm(observe_acu) == 0, "manual re-entry latched as DV (latch not re-decided)"


class TestL013AmsErrorPreemptsDv:
    def test_l013_ams_error_preempts_dv(self, fresh_boot, inv_heartbeat, acu_inject,
                                        udv_inject, pedals, pit_diag, observe_acu, observe_inv,
                                        vcu_profile):
        """L-013: at DV-Active, 0x4A0[0]=5 -> AmsError; 0x360=Off, 0x362=0, 0x511->0."""
        if _setup_dv_active(inv_heartbeat, acu_inject, udv_inject, pedals, observe_acu,
                            vcu_profile) != S.ACTIVE:
            pytest.skip("never reached DV-Active (DAC/gates)")
        udv_inject["set_torque"](40)
        time.sleep(0.3)
        acu_inject["set_ams_state"](5)   # AMS latched Error
        fsm = _wait_fsm(observe_acu, lambda s: s == S.AMS_ERROR, 3.0)
        assert fsm == S.AMS_ERROR, f"did not enter AmsError (fsm {S.name_of(fsm)})"
        time.sleep(0.3)
        mode = _last(observe_inv, M.ID_INV_CMD, M.decode_inv_cmd)
        assert mode is not None and mode["app_state_req"] == int(InvMode.OFF), "0x360 != Off in AmsError"
        assert _torque_nm(observe_inv) == 0, "0x362 non-zero in AmsError"
        assert _r2d_confirm(observe_acu) == 0, "0x511 not cleared by AmsError"


class TestL014UdvDoesNotFeedAmsGate:
    def test_l014_udv_traffic_does_not_hold_ams_fresh(self, fresh_boot, inv_heartbeat,
                                                      acu_inject, udv_inject, pedals, pit_diag,
                                                      observe_acu, vcu_profile):
        """L-014: from WSB, stop 0x020/0x4A0 but keep 0x507+0x510 streaming -> ok_precharge
        bit falls (~210ms) and 0x504 -> 0 (uDV frames must NOT hold AMS freshness alive)."""
        if _setup_wsb(inv_heartbeat, acu_inject, observe_acu, vcu_profile) != S.WAIT_START_BRAKE:
            pytest.skip("never reached WaitStartBrake")
        st = _status(observe_acu)
        assert st["flags"] & M.PIT_FLAG_OK_PRECHARGE, "ok_precharge not set at WSB"
        assert _last(observe_acu, M.ID_DV_TS_ACTIVE, M.decode_ts_active)["ts_active"] == 1

        acu_inject["stop_all"]()          # AMS silent
        udv_inject["set_torque"](30)      # ...but uDV keeps flowing
        udv_inject["set_r2d"](1)
        time.sleep(0.5)                   # > AmsStaleMs (200)
        # The FRESHNESS-GATED view (ci.ok_precharge = veh.ok_precharge && ams_fresh) is on
        # 0x504 ts_active -- it must fall when the AMS goes stale, proving the uDV frames
        # did NOT refresh last_ams_tick. NOTE: the 0x700 ok_precharge FLAG is the RAW
        # veh.ok_precharge (sticky -- pit_diag.cpp:45), so it does NOT track freshness;
        # #101's "0x700 b2.3 falls" assumed a gated flag -> flag to the ECU team.
        assert _last(observe_acu, M.ID_DV_TS_ACTIVE, M.decode_ts_active)["ts_active"] == 0, \
            "0x504 ts_active held alive by uDV traffic (uDV wrongly refreshed AMS freshness)"
