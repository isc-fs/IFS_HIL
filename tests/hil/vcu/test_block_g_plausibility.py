"""
Block G — APPS / brake plausibility, the FSAE EV safety cuts (IFS08-CE-ECU#71).

Drives the FSM to Active, then provokes each cut and watches the 0x700 control
flags + the 0x362 torque command:
  G-001 T.11.8.9 — APPS1/2 disagree > 10% for > 100 ms  -> t11_8_9=1, no torque
  G-002 EV.2.3   — brake pressed (> BrakePressedRaw) + throttle -> ev_2_3=1, no
                   torque, latched until APPS < ~5%
  G-003 implausibly-low / failed APPS (ADC safe-fails to 0) -> no torque

CAVEAT (#71): the APPS / brake thresholds in ecu_config.hpp are COMMISSION
placeholders (final values come from on-car calibration). These tests assert the
plausibility LOGIC against the current placeholders, not the final cal.

Needs ECU_HIL_STUB_START_BTN + start_btn_via_can and a live DAC.
"""
from __future__ import annotations

import time

import pytest

from tools.firmware_test.vcu import can_map as M
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


def _flags(observe_acu):
    f = observe_acu.last(M.ID_PIT_STATUS, extended=False)
    return M.decode_pit_status(f.data).get("flags", 0) if f is not None else 0


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
    s = _wait_fsm(pit_diag, lambda s: s == S.ACTIVE,
                  float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0)
    if s == S.ACTIVE:
        pedals["set_brake"](0)        # release the arming brake
    return s


def _apps(pedals, vcu_profile, p1, p2):
    pedals["set_apps1"](_apps_raw(vcu_profile, "apps1", p1))
    pedals["set_apps2"](_apps_raw(vcu_profile, "apps2", p2))


class TestG001AppsDisagreement:
    def test_g001_t11_8_9_apps_disagreement(self, fresh_boot, inv_heartbeat, acu_inject,
                                            pedals, start_button, pit_diag, observe_acu,
                                            observe_inv, vcu_profile):
        """G-001: APPS1/2 disagree > 10% for > 100 ms -> t11_8_9 set, no torque; clears
        when they re-agree."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- DAC/gates")
        _apps(pedals, vcu_profile, 80, 50)        # 30% apart, well past the 10% limit
        time.sleep(0.4)                           # > the 100 ms persistence window
        assert _flags(observe_acu) & M.PIT_FLAG_T11_8_9, "t11_8_9 not set on APPS disagreement"
        assert abs(_torque(observe_inv) or 0) <= 2, f"torque {_torque(observe_inv)} not cut"
        _apps(pedals, vcu_profile, 80, 80)        # agree again
        time.sleep(0.4)
        assert not (_flags(observe_acu) & M.PIT_FLAG_T11_8_9), "t11_8_9 stuck after re-agree"


class TestG002BrakeThrottle:
    def test_g002_ev_2_3_brake_and_throttle(self, fresh_boot, inv_heartbeat, acu_inject,
                                            pedals, start_button, pit_diag, observe_acu,
                                            observe_inv, vcu_profile):
        """G-002: brake pressed (> BrakePressedRaw) + throttle -> ev_2_3 latched, no torque;
        stays latched until APPS releases."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- DAC/gates")
        _apps(pedals, vcu_profile, 80, 80)
        pedals["set_brake"](3500)                 # > BrakePressedRaw (3000)
        time.sleep(0.4)
        assert _flags(observe_acu) & M.PIT_FLAG_EV_2_3, "ev_2_3 not set on brake+throttle"
        assert abs(_torque(observe_inv) or 0) <= 2, f"torque {_torque(observe_inv)} not cut"
        # EV.2.3 latches: releasing the brake alone must NOT re-enable torque
        pedals["set_brake"](0)
        time.sleep(0.3)
        assert _flags(observe_acu) & M.PIT_FLAG_EV_2_3, "ev_2_3 cleared without APPS release"
        # release APPS -> latch clears
        _apps(pedals, vcu_profile, 0, 0)
        time.sleep(0.4)
        assert not (_flags(observe_acu) & M.PIT_FLAG_EV_2_3), "ev_2_3 stuck after APPS release"


class TestG003ImplausibleApps:
    def test_g003_failed_apps_no_torque(self, fresh_boot, inv_heartbeat, acu_inject,
                                        pedals, start_button, pit_diag, observe_inv,
                                        vcu_profile):
        """G-003: an implausibly-low / failed APPS (the firmware ADC safe-fails to 0,
        not a stale value) commands no torque."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- DAC/gates")
        # both APPS at a raw far below min (sensor disconnect / short to ground / the
        # ADC-error safe-fail) -> clamped to 0% -> no torque.
        pedals["set_apps1"](0)
        pedals["set_apps2"](0)
        time.sleep(0.4)
        assert abs(_torque(observe_inv) or 0) <= 2, \
            f"torque {_torque(observe_inv)} commanded on a failed/low APPS"
