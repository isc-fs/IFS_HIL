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

    @pytest.mark.skip(reason="needs an acu_inject stop-stream hook to make 0x4A0 "
                             "go stale mid-test; deferred")
    def test_f003_stale_ams_fail_safe(self):
        pass
