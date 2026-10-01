"""
Block E — Inverter command sequence (IFS08-CE-ECU#71).

Watches the ECU's inverter setpoints on FDCAN1: 0x360 App_State_Req mode word and
0x362 Torque_Nm_Req. Mode mapping (control.cpp): Off in the early states + R2dDelay,
Ready in WaitInvStandby, TorqueEnable in Active, Fault when inv_state>=10. Torque is
NEGATED (drive = negative Nm) and zero below the 10% deadband.

E-004 (does the real NX/EMC inverter enforce AUTOSAR E2E on RX?) needs a real
inverter and is out of scope for the rig.

Needs the firmware built ECU_HIL_STUB_START_BTN + start_btn_via_can (PB5 jumper open)
and a live DAC for brake/APPS injection.
"""
from __future__ import annotations

import time

import pytest

from tools.firmware_test.vcu import can_map as M
from tools.firmware_test.vcu.can_map import InvMode
from tools.firmware_test.vcu.can_map import VcuFsmState as S


def _wait_fsm(pit_diag, pred, timeout_s):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = pit_diag["read_fsm"](0.3)
        if last is not None and pred(last):
            return last
        time.sleep(0.05)
    return last


def _mode(observe_inv):
    f = observe_inv.last(M.ID_INV_CMD, extended=False)
    return M.decode_inv_cmd(f.data)["app_state_req"] if f is not None else None


def _torque(observe_inv):
    f = observe_inv.last(M.ID_INV_TORQUE, extended=False)
    return M.decode_inv_torque(f.data)["torque_nm"] if f is not None else None


def _apps_raw(vcu_profile, which, pct):
    lo = int(vcu_profile[f"{which}_adc_min"])
    hi = int(vcu_profile[f"{which}_adc_max"])
    return int(lo + pct / 100.0 * (hi - lo))


def _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag, vcu_profile):
    inv_heartbeat["vdc_ready"]()
    acu_inject["set_precharge"](1)
    if _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 4.0) is None:
        pytest.skip("FSM never reached WAIT_START_BRAKE -- DAC/gates")
    pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
    start_button["press"]()
    inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
    return _wait_fsm(pit_diag, lambda s: s == S.ACTIVE,
                     float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0)


class TestE001ModeFollowsFsm:
    def test_e001_off_ready_torqueenable(self, fresh_boot, inv_heartbeat, acu_inject,
                                         pedals, start_button, pit_diag, observe_inv,
                                         vcu_profile):
        """E-001: 0x360 App_State_Req walks Off -> Ready -> TorqueEnable with the FSM."""
        inv_heartbeat["vdc_ready"]()
        acu_inject["set_precharge"](1)
        if _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 4.0) is None:
            pytest.skip("FSM never reached WAIT_START_BRAKE -- DAC/gates")
        time.sleep(0.4)
        assert _mode(observe_inv) == int(InvMode.OFF), \
            f"0x360 mode {_mode(observe_inv)} != Off at WAIT_START_BRAKE"
        # hold inv_state at standby(3) so the FSM parks in WaitInvStandby (mode Ready)
        pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
        start_button["press"]()
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_standby"]))
        got = _wait_fsm(pit_diag, lambda s: s == S.WAIT_INV_STANDBY,
                        float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 4.0)
        assert got == S.WAIT_INV_STANDBY, f"FSM {S.name_of(got)} != WaitInvStandby"
        time.sleep(0.3)
        assert _mode(observe_inv) == int(InvMode.READY), \
            f"0x360 mode {_mode(observe_inv)} != Ready in WaitInvStandby"
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
        assert _wait_fsm(pit_diag, lambda s: s == S.ACTIVE, 4.0) == S.ACTIVE, \
            "FSM never reached Active"
        time.sleep(0.3)
        assert _mode(observe_inv) == int(InvMode.TORQUE_ENABLE), \
            f"0x360 mode {_mode(observe_inv)} != TorqueEnable in Active"


class TestE002TorqueTracksApps:
    def test_e002_torque_tracks_apps(self, fresh_boot, inv_heartbeat, acu_inject,
                                     pedals, start_button, pit_diag, observe_inv,
                                     observe_acu, vcu_profile):
        """E-002: in Active, 0x362 torque tracks APPS (negated); ~0 below the 10%
        deadband; 0x700.torque_cmd mirrors 0x362."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- DAC/gates")
        pedals["set_brake"](0)        # release brake (else EV.2.3 brake+throttle cut)
        pedals["set_apps1"](_apps_raw(vcu_profile, "apps1", 0))
        pedals["set_apps2"](_apps_raw(vcu_profile, "apps2", 0))
        time.sleep(0.6)
        assert abs(_torque(observe_inv) or 0) <= 2, \
            f"torque {_torque(observe_inv)} not ~0 with APPS released"
        # APPS ~80% on both sensors (agree -> no T.11.8.9 trip) -> torque commanded
        pedals["set_apps1"](_apps_raw(vcu_profile, "apps1", 80))
        pedals["set_apps2"](_apps_raw(vcu_profile, "apps2", 80))
        time.sleep(0.6)
        t = _torque(observe_inv)
        assert t is not None and t < -2, f"0x362 torque {t} not commanded on APPS press"
        # The actual torque command is 0x362 (asserted above). 0x700.torque_cmd is
        # currently HARDCODED 0 in firmware (pit_diag.cpp build_status: "inverter
        # unit-map deferred, task #10") -- it does NOT yet mirror 0x362. Assert the
        # documented deferred behaviour; the mirror lands with ECU task #10.
        st = observe_acu.last(M.ID_PIT_STATUS, extended=False)
        tc = M.decode_pit_status(st.data).get("torque_cmd") if st else None
        assert tc == 0, f"0x700.torque_cmd expected 0 (deferred, task #10) but got {tc}"


class TestE003FaultNoTorque:
    def test_e003_inverter_fault_no_torque(self, fresh_boot, inv_heartbeat, acu_inject,
                                           pedals, start_button, pit_diag, observe_inv,
                                           vcu_profile):
        """E-003: inverter fault (inv_state>=10) ⇒ no torque, never TorqueEnable."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- DAC/gates")
        pedals["set_brake"](0)
        pedals["set_apps1"](_apps_raw(vcu_profile, "apps1", 80))
        pedals["set_apps2"](_apps_raw(vcu_profile, "apps2", 80))
        time.sleep(0.5)
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_fault_soft"]))   # 10
        time.sleep(0.6)
        assert _mode(observe_inv) != int(InvMode.TORQUE_ENABLE), \
            "0x360 still TorqueEnable despite inverter fault"
        assert abs(_torque(observe_inv) or 0) <= 2, \
            f"torque {_torque(observe_inv)} commanded despite inverter fault"
