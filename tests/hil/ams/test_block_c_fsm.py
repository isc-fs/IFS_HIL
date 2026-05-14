"""
Block C — FSM transitions.

FSM state is read from byte 0 of the `0x4A0` AMS-status telemetry frame on
FDCAN1 (per `Core/Inc/app/telemetry_encoders.hpp`):

    0 = Start    3 = Run
    1 = Precharge 4 = Charge
    2 = Transition 5 = Error

ACU stimulus drives the transitions:
  - `0x600` standard, byte 0 = 1 → start-button press   (Start → Precharge)
  - `0x18FF50E7` extended         → charger detected     (Start → Charge)
  - `0x100` extended, LE V       → DC bus voltage       (Precharge → Transition,
                                                          Transition → Run / Error)

Implemented:
  HIL-020  Start → Precharge on start button
  HIL-021  Start → Charge on charger detect
  HIL-022  Precharge → Transition on DC-bus target
  HIL-023  Precharge timeout → Error
  HIL-024  Transition → Run after hold
  HIL-025  Transition voltage drop → Error
  HIL-026  Run is terminal (charger toggle ignored)
  HIL-027  Charge is terminal (charger toggle ignored)
  HIL-028  Error is sticky within a boot
"""

from __future__ import annotations

import threading
import time

import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _require_start(wait_for_state, ams_profile):
    """Every Block-C test starts from a known Start state. If the firmware
    came up in any other state, we skip — the bench should be cleanly reset
    between tests (run order matters)."""
    try:
        wait_for_state(M.FsmState.START,
                       timeout_ms=2 * int(ams_profile["tx_telemetry_period_ms"]))
    except AssertionError as e:
        pytest.skip(f"Test prereq: FSM must start in Start. {e}")


@pytest.fixture
def dc_bus_loop(acu):
    """Background thread that emits `0x100` at 50 Hz with a caller-controlled
    voltage. Returns (set_v, stop). The thread terminates on `stop.set()`."""
    state = {"v": 0}
    stop = threading.Event()

    def _run():
        while not stop.is_set():
            acu.send_dc_bus_v(state["v"])
            time.sleep(0.02)

    t = threading.Thread(target=_run, daemon=True, name="dc-bus-loop")
    t.start()

    def set_v(v: int):
        state["v"] = int(v)

    yield set_v
    stop.set()
    t.join(timeout=1.0)


# ---------------------------------------------------------------------------
# HIL-020: Start → Precharge on start button
# ---------------------------------------------------------------------------

class TestStartToPrecharge:
    def test_start_button(self, acu, wait_for_state):
        acu.send_start_button(pressed=True)
        wait_for_state(M.FsmState.PRECHARGE)


# ---------------------------------------------------------------------------
# HIL-021: Start → Charge on charger detect
# ---------------------------------------------------------------------------

class TestStartToCharge:
    def test_charger_detect(self, acu, wait_for_state):
        acu.send_charger_detect()
        wait_for_state(M.FsmState.CHARGE)


# ---------------------------------------------------------------------------
# HIL-022 + HIL-024: Precharge → Transition → Run
# ---------------------------------------------------------------------------

class TestPrechargeToRun:
    def test_precharge_to_transition_to_run(self, acu, dc_bus_loop,
                                            wait_for_state):
        # Enter Precharge
        acu.send_start_button(pressed=True)
        wait_for_state(M.FsmState.PRECHARGE)

        # Drive DC bus high — firmware target is 0.95 × pack ≈ 350 V on a
        # healthy emulator (3700 mV × 19 cells × 5 modules ≈ 350 V)
        dc_bus_loop(360)
        wait_for_state(M.FsmState.TRANSITION)

        # Hold for kTransitionHoldMs (100 ms) → Run
        wait_for_state(M.FsmState.RUN, timeout_ms=M.TRANSITION_HOLD_MS + 200)


# ---------------------------------------------------------------------------
# HIL-023: Precharge timeout → Error
# ---------------------------------------------------------------------------

class TestPrechargeTimeoutError:
    def test_precharge_timeout(self, acu, dc_bus_loop, wait_for_state):
        acu.send_start_button(pressed=True)
        wait_for_state(M.FsmState.PRECHARGE)

        # Hold DC bus well below target for kPrechargeMaxMs + slack
        dc_bus_loop(50)
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=M.PRECHARGE_MAX_MS + 200)


# ---------------------------------------------------------------------------
# HIL-025: Transition voltage drop → Error
# ---------------------------------------------------------------------------

class TestTransitionVoltageDrop:
    def test_v_drop_in_transition(self, acu, dc_bus_loop, wait_for_state):
        acu.send_start_button(pressed=True)
        wait_for_state(M.FsmState.PRECHARGE)

        dc_bus_loop(360)
        wait_for_state(M.FsmState.TRANSITION)

        # Before the 100 ms hold elapses, drop the voltage
        time.sleep(0.030)
        dc_bus_loop(80)

        wait_for_state(M.FsmState.ERROR)


# ---------------------------------------------------------------------------
# HIL-026: Run is terminal (charger toggle ignored)
# ---------------------------------------------------------------------------

class TestRunTerminal:
    def test_run_ignores_charger(self, acu, dc_bus_loop, wait_for_state,
                                 current_state):
        acu.send_start_button(pressed=True)
        wait_for_state(M.FsmState.PRECHARGE)
        dc_bus_loop(360)
        wait_for_state(M.FsmState.TRANSITION)
        wait_for_state(M.FsmState.RUN, timeout_ms=M.TRANSITION_HOLD_MS + 200)

        # Send charger detect — must NOT transition out of Run
        acu.send_charger_detect()
        time.sleep(0.300)
        assert current_state() == M.FsmState.RUN, (
            "Run is terminal: charger-detect frame must not cause a transition. "
            f"Observed state = {M.FsmState.name(current_state())}."
        )


# ---------------------------------------------------------------------------
# HIL-027: Charge is terminal (stopping charger frames must not exit Charge)
# ---------------------------------------------------------------------------

class TestChargeTerminal:
    def test_charge_sticks(self, acu, wait_for_state, current_state):
        acu.send_charger_detect()
        wait_for_state(M.FsmState.CHARGE)

        # Stop sending charger frames; the legacy/refactor design says the
        # charger flag is sticky once seen. Wait a generous 2 s.
        time.sleep(2.0)

        assert current_state() == M.FsmState.CHARGE, (
            f"Charge is terminal: state changed to {M.FsmState.name(current_state())} "
            "after charger frames stopped (expected stay in Charge)."
        )


# ---------------------------------------------------------------------------
# HIL-028: Error is sticky within a boot
# ---------------------------------------------------------------------------

class TestErrorSticky:
    def test_error_sticky(self, bms_emulator, wait_for_state, current_state,
                          ams_profile):
        # Trip Error via cell UV
        bms_emulator.set_cell(module=0, cell=0, mV=M.CELL_UV_MV - 100)
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["error_latch_window_ms"]) + 100)

        # Restore healthy values
        bms_emulator.set_all_cells(int(ams_profile["bms_default_cell_mV"]))
        time.sleep(2.0)

        assert current_state() == M.FsmState.ERROR, (
            "Error should stick across the fault being cleared until a reset. "
            f"Observed state = {M.FsmState.name(current_state())}."
        )
