"""
Block F — AMS-error inhibit (IFS08-CE-ECU#71).

The ECU consumes the AMS status on 0x4A0: byte0 == 5 means the AMS is in its
Error state, and the ECU must inhibit — go to the sticky AmsError FSM state,
command zero torque, and NOT loop precharge-retry. Observed over pit-diag 0x700
(fsm_state + torque_cmd).

F-003 (stale-AMS fail-safe) needs the injector to STOP refreshing 0x4A0 mid-test;
the acu_inject fixture streams continuously, so that's deferred until a
stop-stream hook exists.
"""
from __future__ import annotations

import time

import pytest

from tools.firmware_test.vcu import can_map as M

_AMS_ERROR = int(M.VcuFsmState.AMS_ERROR)


def _wait_fsm(pit_diag, pred, timeout_s):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = pit_diag["read_fsm"]()
        if last is not None and pred(last):
            return last
        time.sleep(0.05)
    return last


class TestF001AmsErrorInhibits:

    def test_f001_ams_error_to_amserror(self, fresh_boot, acu_inject, pit_diag,
                                        observe_acu):
        """F-001: AMS 0x4A0[0]=5 (AMS Error) -> ECU FSM enters AmsError and the
        commanded torque is zero."""
        acu_inject["set_ams_state"](5)
        s = _wait_fsm(pit_diag, lambda v: v == _AMS_ERROR, 4.0)
        assert s == _AMS_ERROR, \
            f"FSM {M.VcuFsmState.name_of(s)} on AMS error, expected AmsError(6)"
        # torque must be zero in the inhibit state (0x700 torque_cmd / torque_pct)
        f = observe_acu.last(M.ID_PIT_STATUS, extended=False)
        assert f is not None
        st = M.decode_pit_status(f.data)
        assert st.get("torque_cmd", 0) == 0 and st.get("torque_pct", 0) == 0, \
            f"non-zero torque in AmsError: cmd={st.get('torque_cmd')} pct={st.get('torque_pct')}"


class TestF002ReArm:

    def test_f002_rearm_on_ams_ok(self, fresh_boot, acu_inject, pit_diag):
        """F-002: when the AMS recovers (0x4A0[0]=0), the ECU leaves AmsError.
        If AmsError is hard-sticky (only clears on reset), this surfaces that as
        a spec/firmware decision."""
        acu_inject["set_ams_state"](5)
        assert _wait_fsm(pit_diag, lambda v: v == _AMS_ERROR, 4.0) == _AMS_ERROR, \
            "never entered AmsError to begin with"
        acu_inject["set_ams_state"](0)
        s = _wait_fsm(pit_diag, lambda v: v != _AMS_ERROR, 4.0)
        assert s != _AMS_ERROR, \
            "FSM stuck in AmsError after AMS returned ok (sticky-until-reset?)"


class TestF003StaleFailSafe:

    def test_f003_stale_ams_fail_safe(self, fresh_boot, inv_heartbeat, acu_inject,
                                       pedals, start_button, pit_diag, observe_acu,
                                       observe_inv, vcu_profile):
        """F-003: stopping all AMS frames (0x020/0x4A0/0x12C) while Active lapses
        AMS-fresh within 200 ms, drops ok_precharge, leaves Active, and cuts torque."""
        inv_heartbeat["vdc_ready"]()
        acu_inject["set_precharge"](1)
        acu_inject["set_ams_state"](3)
        acu_inject["set_v_cell"](3600)
        if _wait_fsm(pit_diag, lambda s: s >= int(M.VcuFsmState.WAIT_START_BRAKE), 4.0) is None:
            pytest.skip("FSM never reached WAIT_START_BRAKE -- DAC/gates")
        pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
        start_button["press"]()
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
        if _wait_fsm(pit_diag, lambda s: s == int(M.VcuFsmState.ACTIVE),
                     float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0) != int(M.VcuFsmState.ACTIVE):
            pytest.skip("FSM never reached Active -- DAC/gates")
        pedals["set_brake"](0)
        lo1, hi1 = int(vcu_profile["apps1_adc_min"]), int(vcu_profile["apps1_adc_max"])
        lo2, hi2 = int(vcu_profile["apps2_adc_min"]), int(vcu_profile["apps2_adc_max"])
        pedals["set_apps1"](int(lo1 + 0.50 * (hi1 - lo1)))
        pedals["set_apps2"](int(lo2 + 0.50 * (hi2 - lo2)))
        time.sleep(0.4)

        f_torq = observe_inv.last(M.ID_INV_TORQUE, extended=False)
        assert f_torq is not None and abs(M.decode_inv_torque(f_torq.data)["torque_nm"]) > 5, \
            "ECU did not command torque in Active before AMS silence"

        # Silence all AMS frames -> > 200 ms AmsStaleMs window
        acu_inject["stop_all"]()
        time.sleep(0.5)

        f_pit = observe_acu.last(M.ID_PIT_STATUS, extended=False)
        assert f_pit is not None
        st = M.decode_pit_status(f_pit.data)
        assert not (st.get("flags", 0) & M.PIT_FLAG_OK_PRECHARGE), \
            "ok_precharge stayed true after AMS went stale"
        assert st.get("fsm_state") != int(M.VcuFsmState.ACTIVE), \
            "ECU stayed ACTIVE after AMS went stale"
        f_torq_after = observe_inv.last(M.ID_INV_TORQUE, extended=False)
        assert f_torq_after is not None and abs(M.decode_inv_torque(f_torq_after.data)["torque_nm"]) <= 2, \
            f"non-zero torque after AMS stale: {M.decode_inv_torque(f_torq_after.data)['torque_nm']}"


class TestF004LowCellDerate:

    def test_f004_low_cell_voltage_derate(self, fresh_boot, inv_heartbeat, acu_inject,
                                          pedals, start_button, pit_diag, observe_inv,
                                          vcu_profile):
        """F-004: 0x12C v_cell_min <= CellVRawFloorMv (2850 mV) caps torque to
        CellVDerateFloorPct (13% / ~20 Nm), and recovers when cell voltage returns."""
        inv_heartbeat["vdc_ready"]()
        acu_inject["set_precharge"](1)
        acu_inject["set_ams_state"](3)
        acu_inject["set_v_cell"](3600)
        if _wait_fsm(pit_diag, lambda s: s >= int(M.VcuFsmState.WAIT_START_BRAKE), 4.0) is None:
            pytest.skip("FSM never reached WAIT_START_BRAKE -- DAC/gates")
        pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
        start_button["press"]()
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
        if _wait_fsm(pit_diag, lambda s: s == int(M.VcuFsmState.ACTIVE),
                     float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0) != int(M.VcuFsmState.ACTIVE):
            pytest.skip("FSM never reached Active -- DAC/gates")
        pedals["set_brake"](0)
        lo1, hi1 = int(vcu_profile["apps1_adc_min"]), int(vcu_profile["apps1_adc_max"])
        lo2, hi2 = int(vcu_profile["apps2_adc_min"]), int(vcu_profile["apps2_adc_max"])
        pedals["set_apps1"](int(lo1 + 0.80 * (hi1 - lo1)))
        pedals["set_apps2"](int(lo2 + 0.80 * (hi2 - lo2)))
        time.sleep(0.5)

        f_base = observe_inv.last(M.ID_INV_TORQUE, extended=False)
        assert f_base is not None
        base_nm = abs(M.decode_inv_torque(f_base.data)["torque_nm"])
        assert base_nm > 60, f"expected high torque at 80% APPS, got {base_nm} Nm"

        # Drop v_cell_min below CellVRawFloorMv (2850 mV) -> raw backstop caps to 13% (~20 Nm)
        acu_inject["set_v_cell"](2840)
        time.sleep(0.5)
        f_derate = observe_inv.last(M.ID_INV_TORQUE, extended=False)
        assert f_derate is not None
        derate_nm = abs(M.decode_inv_torque(f_derate.data)["torque_nm"])
        assert 5 <= derate_nm < base_nm * 0.5, \
            f"low-cell derate did not cap torque: base={base_nm} Nm, derated={derate_nm} Nm"

        # Recover cell voltage -> trim filter climbs back above knee
        acu_inject["set_v_cell"](3600)
        time.sleep(3.0)
        f_rec = observe_inv.last(M.ID_INV_TORQUE, extended=False)
        assert f_rec is not None
        rec_nm = abs(M.decode_inv_torque(f_rec.data)["torque_nm"])
        assert rec_nm > derate_nm + 20, \
            f"torque did not recover after v_cell_min restored: derated={derate_nm}, rec={rec_nm}"


class TestF005AmsSessionGuard:

    def test_f005_ams_session_change_dearms(self, fresh_boot, inv_heartbeat, acu_inject,
                                            pedals, start_button, pit_diag, observe_inv,
                                            vcu_profile):
        """F-005: mid-run 0x4A0[3] session_id change (AMS reboot guard) de-arms ECU
        out of Active and cuts torque to zero."""
        inv_heartbeat["vdc_ready"]()
        acu_inject["set_precharge"](1)
        acu_inject["set_ams_state"](3)
        acu_inject["set_session"](1)
        acu_inject["set_v_cell"](3600)
        if _wait_fsm(pit_diag, lambda s: s >= int(M.VcuFsmState.WAIT_START_BRAKE), 4.0) is None:
            pytest.skip("FSM never reached WAIT_START_BRAKE -- DAC/gates")
        pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
        start_button["press"]()
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
        if _wait_fsm(pit_diag, lambda s: s == int(M.VcuFsmState.ACTIVE),
                     float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0) != int(M.VcuFsmState.ACTIVE):
            pytest.skip("FSM never reached Active -- DAC/gates")
        pedals["set_brake"](0)
        lo1, hi1 = int(vcu_profile["apps1_adc_min"]), int(vcu_profile["apps1_adc_max"])
        lo2, hi2 = int(vcu_profile["apps2_adc_min"]), int(vcu_profile["apps2_adc_max"])
        pedals["set_apps1"](int(lo1 + 0.50 * (hi1 - lo1)))
        pedals["set_apps2"](int(lo2 + 0.50 * (hi2 - lo2)))
        time.sleep(0.4)

        # Simulate AMS reboot with new session ID
        acu_inject["set_session"](2)
        s = _wait_fsm(pit_diag, lambda v: v != int(M.VcuFsmState.ACTIVE), 2.0)
        assert s != int(M.VcuFsmState.ACTIVE), "ECU remained ACTIVE after AMS session_id changed"
        f_torq = observe_inv.last(M.ID_INV_TORQUE, extended=False)
        assert f_torq is not None and abs(M.decode_inv_torque(f_torq.data)["torque_nm"]) <= 2, \
            "non-zero torque after AMS session_id change"
