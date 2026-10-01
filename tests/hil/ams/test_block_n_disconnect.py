"""
Block N — pack current-sensor DISCONNECT detection (AMS #355).

PF7/PF8 carry a weak internal pull-down (GPIO PUPDR). Connected, the SSA-2
op-amp output overrides it; unplugged, the legs collapse toward 0 V. The
firmware reads OUT_P (PF7) single-ended every 50 ms and faults if it leaves
[CurrentLegPlausMinMv, CurrentLegPlausMaxMv] = [700, 2300] mV for
CurrentDisconnectConfirm = 3 consecutive reads (~150 ms) -> CurrentSensorFault
(reason 8) -> Error, AIRs open (FSM opens contactors on Error, see F-066),
AMS_OK low.

Driven via pack_current_diff (DAC4 ch0 = OUT_P/PF7, ch1 = OUT_N/PF8);
`set_legs(vp, vn)` drives the legs independently for the disconnect rows.

| ID    | What it checks                                                    |
|-------|-------------------------------------------------------------------|
| N-001 | OUT_P -> 0 V -> reason 8 (CurrentSensorFault), AMS_OK low, Error   |
| N-002 | reconnect (legs -> CM) -> stays latched (sticky)                  |
| N-003 | normal sweep 0..±190 A -> never reason 8 (OUT_P stays in window)   |
| N-004 | OUT_N -> 0 V (OUT_P in window) -> reason 10 (CurrentOverLimit)     |

SCOPE — firmware-response only: these drive OUT_P/OUT_N to 0 V to prove the
firmware's window-check + FSM response. The *physical* pull-down gate (does a
real unplug drag OUT_P to ~0 V — the H7 PUPDR on an analog pin) needs a
physical OUT_P unplug; the broker cannot Hi-Z the DAC. That is the #355 gate,
verified by hand separately.
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M

_REASON_SENSOR_FAULT = 8     # ams_config FaultReason::CurrentSensorFault
_REASON_OVER_LIMIT   = 10    # ams_config FaultReason::CurrentOverLimit
_CM_V = 1.44                 # leg common-mode: 0 A differential, OUT_P in-window


def _require_diff(pack_current_diff):
    if not pack_current_diff.get("enabled"):
        pytest.skip("pack_current_diff disabled: pack_current_dac_* absent.")


def _fsm(pit_diag):
    """(fault_reason, fsm_state, ams_ok) from pit-diag 0x6C0 [6]/[0]/[3]."""
    pit_diag.wait_for_scan()
    f = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
    return f[6], f[0], f[3]


def _trip_window(ams_profile, mult=2):
    return int(ams_profile["tx_telemetry_period_ms"]) * mult + 500


class TestBlockNDisconnect:

    def test_n001_outp_disconnect_latches_reason8(self, fresh_boot_diff,
            pack_current_diff, wait_for_settled, wait_for_state, pit_diag,
            ams_profile):
        """N-001: OUT_P (PF7) -> 0 V (below the 700 mV floor) -> within ~150 ms
        the firmware latches CurrentSensorFault (reason 8) -> Error, AMS_OK low."""
        _require_diff(pack_current_diff)
        assert fresh_boot_diff["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()
        pack_current_diff["set_legs"](0.0, _CM_V)     # OUT_P collapses
        wait_for_state(M.FsmState.ERROR, timeout_ms=_trip_window(ams_profile))
        reason, _state, ams_ok = _fsm(pit_diag)
        assert reason == _REASON_SENSOR_FAULT, \
            f"fault_reason={reason}, want 8 (CurrentSensorFault) on OUT_P disconnect"
        assert ams_ok == 0, "AMS_OK must be low on a sensor disconnect"
        pack_current_diff["set_legs"](_CM_V, _CM_V)

    def test_n002_disconnect_latch_sticky(self, fresh_boot_diff, pack_current_diff,
            wait_for_settled, wait_for_state, pit_diag, ams_profile):
        """N-002: after a disconnect trip, reconnecting (legs -> CM) must NOT
        clear it — the latch is sticky until a reset."""
        _require_diff(pack_current_diff)
        assert fresh_boot_diff["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()
        pack_current_diff["set_legs"](0.0, _CM_V)
        wait_for_state(M.FsmState.ERROR, timeout_ms=_trip_window(ams_profile))
        pack_current_diff["set_legs"](_CM_V, _CM_V)   # reconnect
        time.sleep(max(1.0, int(ams_profile["tx_telemetry_period_ms"]) * 3 / 1000.0))
        reason, state, _ = _fsm(pit_diag)
        assert state == M.FsmState.ERROR, \
            f"reconnect cleared the latch (state={M.FsmState.name(state)}); must stay Error"
        assert reason == _REASON_SENSOR_FAULT, f"latched reason={reason}, want a sticky 8"

    def test_n003_normal_range_no_false_trip(self, fresh_boot_diff, pack_current_diff,
            wait_for_settled, pit_diag, ams_profile):
        """N-003: sweep the pack current across the normal range; OUT_P stays
        inside the plausibility window so reason 8 never fires."""
        _require_diff(pack_current_diff)
        assert fresh_boot_diff["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()
        settle_s = float(ams_profile.get("pack_current_settle_s", 4.0))
        for amps in (0, 100, 190, -100, -190, 0):
            pack_current_diff["set_A"](amps)
            time.sleep(settle_s)
            reason, _state, _ = _fsm(pit_diag)
            assert reason != _REASON_SENSOR_FAULT, \
                f"{amps:+d} A (normal range) tripped reason 8 — OUT_P left the window?"
        pack_current_diff["set_A"](0)

    def test_n004_outn_open_is_overlimit_not_sensorfault(self, fresh_boot_diff,
            pack_current_diff, wait_for_settled, wait_for_state, pit_diag,
            ams_profile):
        """N-004: OUT_N (PF8) -> 0 V with OUT_P in-window -> the differential
        skews to a huge current -> CurrentOverLimit (reason 10), NOT reason 8.
        sensor_fault is evaluated before over-limit, so OUT_P-in-window means
        reason 10 wins — confirming the two predicates cover distinct modes."""
        _require_diff(pack_current_diff)
        assert fresh_boot_diff["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()
        pack_current_diff["set_legs"](_CM_V, 0.0)     # OUT_N collapses, OUT_P mid
        wait_for_state(M.FsmState.ERROR, timeout_ms=_trip_window(ams_profile, 4))
        reason, _state, _ = _fsm(pit_diag)
        assert reason == _REASON_OVER_LIMIT, \
            f"OUT_N open gave reason={reason}, want 10 (CurrentOverLimit) — OUT_P in window"
        pack_current_diff["set_legs"](_CM_V, _CM_V)
