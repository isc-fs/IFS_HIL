"""
Block E (fault recovery) — inverter fault-recovery sequence (IFS08-CE-ECU#94 E-003..E-007).

The NX inverter can boot LATCHED in a fault (App_State 10/11). The ECU commands its
recovery mode word REACTIVELY in any non-AmsError state (control.cpp:162):
  inv_state 11 (hard fault) -> 0x360 b2 = 0x0D HardFaultReset
  inv_state 10 (soft fault) -> 0x360 b2 = 0x13 Fault
In AmsError the recovery is suppressed (Off, 0x01). The recovery is what lets the
FSM clear a boot-latched inverter and still reach Active.

E-003/4/7 are CAN-only; E-005/6 drive to Active (need a live DAC).
"""
from __future__ import annotations

import time

import pytest

from tools.firmware_test.vcu import can_map as M
from tools.firmware_test.vcu.can_map import InvMode
from tools.firmware_test.vcu.can_map import VcuFsmState as S

AMS_ERROR_STATE = 5   # 0x4A0 byte0 == 5 -> AMS latched Error (AmsFsmError)


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


class TestE003HardFaultRecovery:
    def test_e003_hard_fault_commands_reset(self, fresh_boot, inv_heartbeat, observe_inv,
                                            vcu_profile):
        """E-003: inv_state=11 (hard fault) -> 0x360 b2 = HardFaultReset (0x0D)."""
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_fault_hard"]))   # 11
        time.sleep(0.6)
        assert _mode(observe_inv) == int(InvMode.HARD_FAULT_RESET), \
            f"0x360 mode {hex(_mode(observe_inv) or 0)} != HardFaultReset (0x0D)"


class TestE004SoftFaultRecovery:
    def test_e004_soft_fault_commands_fault(self, fresh_boot, inv_heartbeat, observe_inv,
                                            vcu_profile):
        """E-004: inv_state=10 (soft fault) -> 0x360 b2 = Fault (0x13)."""
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_fault_soft"]))   # 10
        time.sleep(0.6)
        assert _mode(observe_inv) == int(InvMode.FAULT), \
            f"0x360 mode {hex(_mode(observe_inv) or 0)} != Fault (0x13)"


class TestE005RecoveryAdvances:
    def test_e005_recovery_clears_and_fsm_advances(self, fresh_boot, inv_heartbeat,
                                                   acu_inject, pedals, start_button,
                                                   pit_diag, observe_inv, vcu_profile):
        """E-005: a boot-latched hard fault (11) is commanded 0x0D; once the inverter
        recovers (11 -> standby 3 -> ready 4) the FSM reaches Active (no stall at 4)."""
        inv_heartbeat["vdc_ready"]()
        acu_inject["set_precharge"](1)
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_fault_hard"]))   # boot latched
        if _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 4.0) is None:
            pytest.skip("FSM never reached WAIT_START_BRAKE -- DAC/gates")
        time.sleep(0.3)
        assert _mode(observe_inv) == int(InvMode.HARD_FAULT_RESET), \
            "ECU not commanding 0x0D while the inverter is hard-faulted"
        pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
        start_button["press"]()
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_standby"]))      # 3, recovering
        time.sleep(0.5)
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))        # 4, recovered
        active = _wait_fsm(pit_diag, lambda s: s == S.ACTIVE,
                           float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0)
        assert active == S.ACTIVE, \
            f"FSM stalled at {S.name_of(active)} -- recovery didn't let it advance"


class TestE006FaultCutsTorque:
    def test_e006_fault_in_active_cuts_torque(self, fresh_boot, inv_heartbeat, acu_inject,
                                              pedals, start_button, pit_diag, observe_inv,
                                              vcu_profile):
        """E-006: a fault while driving (Active + APPS) -> 0x362 torque 0 + 0x360 = 0x0D."""
        inv_heartbeat["vdc_ready"]()
        acu_inject["set_precharge"](1)
        if _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 4.0) is None:
            pytest.skip("FSM never reached WAIT_START_BRAKE -- DAC/gates")
        pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
        start_button["press"]()
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
        if _wait_fsm(pit_diag, lambda s: s == S.ACTIVE,
                     float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- DAC/gates")
        pedals["set_brake"](0)
        pedals["set_apps1"](_apps_raw(vcu_profile, "apps1", 80))
        pedals["set_apps2"](_apps_raw(vcu_profile, "apps2", 80))
        time.sleep(0.6)
        assert (_torque(observe_inv) or 0) < -2, "no torque commanded before the fault"
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_fault_hard"]))   # 11
        time.sleep(0.5)
        assert abs(_torque(observe_inv) or 0) <= 2, \
            f"torque {_torque(observe_inv)} not cut on the inverter fault"
        assert _mode(observe_inv) == int(InvMode.HARD_FAULT_RESET), \
            "not commanding 0x0D on the fault"


class TestE007AmsErrorSuppressesRecovery:
    def test_e007_ams_error_suppresses_recovery(self, fresh_boot, inv_heartbeat, acu_inject,
                                                pit_diag, observe_inv, vcu_profile):
        """E-007: in AmsError, an inverter fault must NOT command recovery -- the safe
        command is Off (0x01), not 0x0D."""
        acu_inject["set_ams_state"](AMS_ERROR_STATE)   # 0x4A0[0]=5 -> AmsError
        if _wait_fsm(pit_diag, lambda s: s == S.AMS_ERROR, 4.0) != S.AMS_ERROR:
            pytest.skip("FSM never entered AmsError")
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_fault_hard"]))   # 11
        time.sleep(0.5)
        assert _mode(observe_inv) == int(InvMode.OFF), \
            f"0x360 mode {hex(_mode(observe_inv) or 0)} != Off (0x01) -- recovery not suppressed in AmsError"
