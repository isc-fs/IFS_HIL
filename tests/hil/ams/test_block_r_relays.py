"""
Block R — relay-status telemetry (0x4A4), AMS v1.6.2 (#395 / #397).

The always-on 0x4A4 AMS_relay_status frame (AMS PR #395) carries ODR
read-backs of what the firmware drives the contactor coils + AMS_OK GPIO
to, in byte 0: bit0 air_negative, bit1 air_positive, bit2 precharge,
bit3 ams_ok. This is the direct contactor-sequence observability the
pit-diag stream never gave us.

| ID    | What it checks                                                       |
|-------|----------------------------------------------------------------------|
| R-110 | Start: bits 0,0,0,1 (AIRs + precharge open, AMS_OK high post-grace)   |
| R-111 | ams_ok bit (3) tracks 0x6C0[3] across healthy / latched-Error        |
| R-112 | Car Precharge closes the resistor (air-,precharge=1); proceed → air+ |
| R-113 | **#397 guard:** Charger Precharge SKIPS the resistor — precharge      |
|       | bit never goes 1 through Start→Precharge→Charge                       |
| R-114 | Run steady: air-,air+ =1, precharge=0                                |
| R-115 | Error latch → all contactor bits 0 within a tick; ams_ok follows     |

Reuses Block C's FSM drive helpers; 0x4A4 is decoded straight off the bus.
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M
from tests.hil.ams.test_block_c_fsm import (
    _require_inputs, _drive_to_precharge, _drive_to_run, _drive_to_charge,
)


def _relay(observe_acu):
    """Decoded last 0x4A4, or skip if the frame is absent (pre-#395 image)."""
    f = observe_acu.last(M.ID_RELAY_STATUS, extended=False)
    if f is None:
        pytest.skip("no 0x4A4 AMS_relay_status frame — firmware predates #395")
    return M.decode_relay_status(f.data)


def _bits(r):
    return (r["air_negative"], r["air_positive"], r["precharge"], r["ams_ok"])


def _win(ams_profile, mult=1):
    return int(ams_profile["state_transition_window_ms"]) * mult + 150


class TestBlockRRelayStatus:

    # R-110 -----------------------------------------------------------------
    def test_r110_start_all_open_amsok_high(self, fresh_boot, wait_for_settled,
                                            observe_acu):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START
        wait_for_settled()
        assert _bits(_relay(observe_acu)) == (False, False, False, True), \
            "Start: expect AIRs + precharge open, AMS_OK high"

    # R-111 -----------------------------------------------------------------
    def test_r111_ams_ok_bit_tracks_pit_diag(self, fresh_boot, wait_for_settled,
            pico_emu, pit_diag, observe_acu, wait_for_state, ams_profile):
        wait_for_settled()
        # healthy: 0x4A4 bit3 == 0x6C0[3] == high
        pit_diag.wait_for_scan()
        fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        assert _relay(observe_acu)["ams_ok"] is True
        assert _relay(observe_acu)["ams_ok"] == bool(fsm[3]), "ams_ok bit != 0x6C0[3] healthy"
        # latched Error (cell-UV) -> ams_ok low, both frames agree
        pico_emu.inject_cell_v(module=0, cell=10, mV=2500)   # < 2800
        try:
            wait_for_state(M.FsmState.ERROR, timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 500)
            assert _relay(observe_acu)["ams_ok"] is False, "ams_ok bit must drop on Error latch"
        finally:
            pico_emu.set_all_cells(3750)

    # R-112 -----------------------------------------------------------------
    def test_r112_car_precharge_closes_resistor(self, fresh_boot, tsms, dash_chg,
            acu_heartbeat, wait_for_state, observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)
        r = _relay(observe_acu)
        assert r["air_negative"] and r["precharge"] and not r["air_positive"], \
            f"Car Precharge expects air-,precharge closed / air+ open; got {r}"
        # proceed to Run via the RC bus ramp
        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        acu_heartbeat["ramp_to"](pack_V)
        wait_for_state(M.FsmState.RUN, timeout_ms=int(ams_profile["transition_hold_ms"]) + _win(ams_profile))
        r2 = _relay(observe_acu)
        assert r2["air_positive"] and not r2["precharge"], \
            f"Car Run expects air+ closed / precharge open; got {r2}"

    # R-113 — the #397 precharge-skip regression guard ----------------------
    def test_r113_charger_precharge_is_skipped(self, fresh_boot, tsms, dash_chg,
            acu_heartbeat, charger_0x101, wait_for_state, observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        t0 = time.monotonic()
        _drive_to_charge(tsms, dash_chg, acu_heartbeat, charger_0x101,
                         wait_for_state, ams_profile)
        # the precharge bit must NEVER read 1 across the whole Start->Charge path
        seen = observe_acu.frames(M.ID_RELAY_STATUS, since=t0)
        bad = [M.decode_relay_status(f.data) for f in seen
               if M.decode_relay_status(f.data)["precharge"]]
        assert not bad, f"#397 VIOLATED: precharge contactor closed in Charger path ({len(bad)} frames)"
        # AIR- closed without precharge; on the proceed AIR+ closes, precharge still 0
        r = _relay(observe_acu)
        assert r["air_negative"] and not r["precharge"], f"Charge: air- closed, precharge open; got {r}"

    # R-114 -----------------------------------------------------------------
    def test_r114_run_steady_both_airs_closed(self, fresh_boot, tsms, dash_chg,
            acu_heartbeat, wait_for_state, observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile)
        r = _relay(observe_acu)
        assert r["air_negative"] and r["air_positive"] and not r["precharge"], \
            f"Run steady expects both AIRs closed, precharge open; got {r}"

    # R-115 -----------------------------------------------------------------
    def test_r115_error_opens_all_contactors(self, fresh_boot, tsms, dash_chg,
            acu_heartbeat, pico_emu, wait_for_state, observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile)
        pico_emu.inject_cell_v(module=0, cell=10, mV=2500)   # cell-UV -> Error
        try:
            wait_for_state(M.FsmState.ERROR, timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 2 + 500)
            r = _relay(observe_acu)
            assert _bits(r) == (False, False, False, False), \
                f"Error must drop all contactor bits + AMS_OK; got {r}"
        finally:
            pico_emu.set_all_cells(3750)
