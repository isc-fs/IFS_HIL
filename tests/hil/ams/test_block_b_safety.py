"""
Block B — Safety supervisor (subset that's bench-observable).

Without external access to PD3/PD4/PD5 (relays), PE9 (SDC), PF11 (current
ADC), or PG7 (charge button) on this MLC-only stack, we can only test
safety predicates that the bench can stimulate via the BMS emulator on
FDCAN2, and observe the firmware's response via the FSM-state byte in the
0x4A0 telemetry frame on FDCAN1.

Implemented:
  HIL-014  Cell undervoltage trips ERROR
  HIL-015  Cell overvoltage trips ERROR
  HIL-016  Cell overtemperature trips ERROR
  HIL-017  BMS module staleness trips ERROR

Deferred (need external GPIO access or GDB):
  HIL-010  SafetyTask 10 ms cadence
  HIL-011  IWDG resets the chip if SafetyTask hangs
  HIL-012  Watchdog reset re-opens relays
  HIL-013  FORCE_ERROR event flag opens AIRs
  HIL-018  Current sensor staleness
  HIL-019  SDC open trips ERROR
"""

from __future__ import annotations

import time

import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# All Block B tests run with the AMS app already on the carrier.
# Test order: bring the firmware up first (relies on Block A having flashed
# a `.bin` to the app slot at some prior point).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _require_running_app(observe_acu, ams_profile):
    """Skip if no 0x4A0 telemetry is observed within the start-up window.
    Implies the carrier doesn't have the AMS app installed (run Block A first)."""
    import time
    deadline = time.time() + 1.5
    while time.time() < deadline:
        if observe_acu.last(M.ID_TELEM_STATUS, extended=False) is not None:
            return
        time.sleep(0.05)
    pytest.skip("No AMS telemetry on FDCAN1 — flash the app via Block A first")


# ---------------------------------------------------------------------------
# HIL-014: cell undervoltage trips ERROR
# ---------------------------------------------------------------------------

class TestCellUndervoltage:
    def test_uv_trips_error(self, bms_emulator, observe_acu, wait_for_state,
                            ams_profile):
        # Establish baseline: pack healthy, expect not-Error
        bms_emulator.set_all_cells(int(ams_profile["bms_default_cell_mV"]))
        time.sleep(0.5)
        baseline = observe_acu.last(M.ID_TELEM_STATUS).data
        assert M.decode_telem_status(baseline)["state"] != M.FsmState.ERROR, (
            "Pre-test FSM is already in Error — clear residual fault before retesting."
        )

        # Inject a single undervoltage cell
        bms_emulator.set_cell(module=2, cell=5, mV=M.CELL_UV_MV - 100)

        snap = wait_for_state(M.FsmState.ERROR,
                              timeout_ms=int(ams_profile["error_latch_window_ms"]) + 100)
        assert snap["min_cell_mV"] <= M.CELL_UV_MV, (
            f"FSM entered Error but min_cell_mV in 0x4A0 was {snap['min_cell_mV']} "
            f"(expected ≤ {M.CELL_UV_MV})."
        )


# ---------------------------------------------------------------------------
# HIL-015: cell overvoltage trips ERROR
# ---------------------------------------------------------------------------

class TestCellOvervoltage:
    def test_ov_trips_error(self, bms_emulator, wait_for_state, ams_profile):
        bms_emulator.set_all_cells(int(ams_profile["bms_default_cell_mV"]))
        time.sleep(0.5)

        bms_emulator.set_cell(module=1, cell=10, mV=M.CELL_OV_MV + 50)

        snap = wait_for_state(M.FsmState.ERROR,
                              timeout_ms=int(ams_profile["error_latch_window_ms"]) + 100)
        assert snap["max_cell_mV"] >= M.CELL_OV_MV, (
            f"FSM entered Error but max_cell_mV in 0x4A0 was {snap['max_cell_mV']} "
            f"(expected ≥ {M.CELL_OV_MV})."
        )


# ---------------------------------------------------------------------------
# HIL-016: cell overtemperature trips ERROR
# ---------------------------------------------------------------------------

class TestCellOvertemperature:
    def test_ot_trips_error(self, bms_emulator, observe_acu, wait_for_state,
                            ams_profile):
        bms_emulator.set_all_temps(int(ams_profile["bms_default_temp_C"]))
        time.sleep(0.5)

        bms_emulator.set_temp(module=3, sensor=15, C=M.CELL_OT_C + 5)

        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["error_latch_window_ms"]) + 200)

        # Cross-check via the 0x4A2 telemetry that max_tempC reflects the injected sensor
        temps_frame = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        if temps_frame is not None:
            t = M.decode_telem_temps(temps_frame.data)
            assert t["max_tempC"] >= M.CELL_OT_C, (
                f"FSM entered Error but max_tempC in 0x4A2 was {t['max_tempC']} "
                f"(expected ≥ {M.CELL_OT_C})."
            )


# ---------------------------------------------------------------------------
# HIL-017: BMS module staleness trips ERROR
# ---------------------------------------------------------------------------

class TestBmsStaleness:
    def test_stop_module_3_trips_error(self, bms_emulator, wait_for_state):
        # Healthy baseline
        time.sleep(0.5)
        # Simulate dead slave — emulator stops responding for module 3
        bms_emulator.stop_module(3)

        # Firmware staleness threshold is 1500 ms; allow 500 ms slack
        wait_for_state(M.FsmState.ERROR, timeout_ms=2000)
