"""
Block C — VCU startup FSM & precharge handshake (IFS08-CE-ECU#35).

Drives the control.c FSM through its gates, observing state via pit-diag
0x700[0]. FSM enum (control.c): WAIT_INV_VDC_CONFIG=0 .. ACTIVE=6.
SIL ref: --test-full-cycle, --test-precharge-no-ack.

Gates confirmed in firmware:
  - vdc gate  = ANY 0x466 received (inv_vdc_ready, can.c:134)
  - precharge = 0x020[0] != 0 (ok_precarga; the ≥300 V shortcut was REMOVED)
  - R2D       = start button && brake > UMBRAL_FRENO_ARRANQUE
  - ACTIVE    = inv_state == 3

C-003 (R2D needs button + brake) is exercised inside the C-004 full sequence.
Tests skip cleanly if pit-diag 0x700 isn't on the configured bus (placeholder).
"""
from __future__ import annotations

import time

import pytest

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


class TestC001VdcConfigGate:
    def test_c001_vdc_gate(self, fresh_boot, inv_heartbeat, pit_diag):
        """C-001: without any 0x466 the FSM holds in WAIT_INV_VDC_CONFIG (0);
        injecting 0x466 (inv_vdc_ready) advances it."""
        held = _wait_fsm(pit_diag, lambda s: s == S.WAIT_INV_VDC_CONFIG, 2.0)
        if held is None:
            pytest.skip("no pit-diag 0x700 on the configured bus -- confirm pit_diag_bus")
        assert held == S.WAIT_INV_VDC_CONFIG, \
            f"expected WAIT_INV_VDC_CONFIG, got {S.name_of(held)}"
        inv_heartbeat["vdc_ready"]()
        adv = _wait_fsm(pit_diag, lambda s: s != S.WAIT_INV_VDC_CONFIG, 3.0)
        assert adv != S.WAIT_INV_VDC_CONFIG, "FSM did not leave WAIT_INV_VDC_CONFIG after 0x466"


class TestC002PrechargeGate:
    def test_c002_precharge_only_gate(self, fresh_boot, inv_heartbeat,
                                      acu_inject, pit_diag):
        """C-002: past the vdc gate, the FSM does NOT reach WAIT_START_BRAKE
        without ok_precarga; it advances once 0x020[0]=1."""
        inv_heartbeat["vdc_ready"]()
        stuck = _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 2.5)
        if stuck is None:
            pytest.skip("no pit-diag 0x700 -- confirm pit_diag_bus")
        assert stuck < S.WAIT_START_BRAKE, \
            f"reached {S.name_of(stuck)} without ok_precarga -- precharge gate not enforced"
        acu_inject["set_precharge"](1)
        adv = _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 4.0)
        assert adv is not None and adv >= S.WAIT_START_BRAKE, \
            f"FSM stuck at {S.name_of(adv)} after ok_precarga"


class TestC004ReachesActive:
    def test_c004_full_startup_to_active(self, fresh_boot, inv_heartbeat,
                                         acu_inject, pedals, start_button,
                                         pit_diag, vcu_profile):
        """C-004 (+C-003): vdc → precharge → brake+button → RTDS delay →
        inv_state==InvReadyState(4) drives the FSM all the way to ACTIVE.
        WaitInvStandby→Active gates on inv_state==4 (control.cpp), NOT the
        standby value 3 — feeding 3 stalls the FSM one step short."""
        inv_heartbeat["vdc_ready"]()
        acu_inject["set_precharge"](1)
        if _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 4.0) is None:
            pytest.skip("FSM never reached WAIT_START_BRAKE -- confirm pit-diag bus / gates")
        pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
        start_button["press"]()
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
        active = _wait_fsm(pit_diag, lambda s: s == S.ACTIVE,
                           float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0)
        assert active == S.ACTIVE, f"FSM reached {S.name_of(active)}, not ACTIVE"


class TestC005FsmObservable:
    def test_c005_pit_diag_reflects_fsm(self, fresh_boot, inv_heartbeat, pit_diag):
        """C-005: pit-diag 0x700[0] reports a valid FSM state."""
        s = pit_diag["read_fsm"](2.0)
        if s is None:
            pytest.skip("no pit-diag 0x700 -- confirm pit_diag_bus")
        assert 0 <= s <= int(S.ACTIVE), f"0x700[0]={s} not a valid FSM state"
