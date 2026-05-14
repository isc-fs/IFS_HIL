"""
Block D — Communications.

CAN cadence + parsing tests. Now that the firmware emits classic CAN frames
(FDCAN_FRAME_CLASSIC), the bench MCP2515s can decode TX traffic and we can
assert on both cadence and payload.

Implemented:
  HIL-030  BMS voltage poll cadence (250 ms)
  HIL-031  BMS temp poll cadence    (500 ms)
  HIL-032  BMS voltage parsing — emulator → firmware → 0x4A0 min/max
  HIL-033  BMS temperature parsing — emulator → firmware → 0x4A2 min/max
  HIL-035  ACU 0x100 DC bus parsing — bench → firmware → 0x4A2 dc_bus_V
  HIL-036  ACU 0x600 start button — observed via state transition
  HIL-037  ACU charger-detect frame — observed via state transition
  HIL-038  ACU TX min cell V cadence (0x12C ext)
  HIL-040  Charger-suppress: 0x12C ext and BMS polls stop in Charge

Deferred:
  HIL-034  Unknown BMS frame counter — firmware doesn't expose via telemetry
  HIL-039  ACU TX current cadence — PF11 current ADC isn't routed; can't
           inject a known current to verify TX payload
"""

from __future__ import annotations

import time

import pytest

from tools.firmware_test.ams import can_map as M


@pytest.fixture(autouse=True)
def _require_running_app(observe_acu, ams_profile):
    import time
    deadline = time.time() + 1.5
    while time.time() < deadline:
        if observe_acu.last(M.ID_TELEM_STATUS, extended=False) is not None:
            return
        time.sleep(0.05)
    pytest.skip("No AMS telemetry on FDCAN1 — flash the app via Block A first")


# ---------------------------------------------------------------------------
# HIL-030: BMS voltage poll cadence
# ---------------------------------------------------------------------------

class TestBmsVoltagePollCadence:
    def test_each_module_polled_250ms(self, observe_bms, bms_emulator,
                                      ams_profile):
        observe_bms.clear()
        time.sleep(2.5)   # capture 10 cycles
        for m in range(M.NUM_BMS_MODULES):
            period = observe_bms.mean_period_ms(M.bms_voltage_poll_id(m))
            assert period is not None, f"No voltage polls for module {m}"
            target = ams_profile["bms_voltage_poll_period_ms"]
            jitter = ams_profile["bms_voltage_poll_jitter_ms"]
            assert abs(period - target) < jitter, (
                f"Module {m} voltage-poll cadence = {period:.0f} ms, "
                f"expected {target} ± {jitter} ms"
            )


# ---------------------------------------------------------------------------
# HIL-031: BMS temp poll cadence
# ---------------------------------------------------------------------------

class TestBmsTempPollCadence:
    def test_each_module_temp_polled_500ms(self, observe_bms, bms_emulator,
                                           ams_profile):
        observe_bms.clear()
        time.sleep(3.0)   # capture ~6 cycles
        for m in range(M.NUM_BMS_MODULES):
            period = observe_bms.mean_period_ms(M.bms_temp_poll_id(m))
            assert period is not None, f"No temp polls for module {m}"
            target = ams_profile["bms_temp_poll_period_ms"]
            jitter = ams_profile["bms_temp_poll_jitter_ms"]
            assert abs(period - target) < jitter, (
                f"Module {m} temp-poll cadence = {period:.0f} ms, "
                f"expected {target} ± {jitter} ms"
            )


# ---------------------------------------------------------------------------
# HIL-032: BMS voltage parsing round-trips through firmware
# ---------------------------------------------------------------------------

class TestBmsVoltageParsing:
    def test_min_max_reflect_emulator_values(self, bms_emulator, observe_acu):
        # Drive a known min/max pair via the emulator
        bms_emulator.set_all_cells(3700)
        bms_emulator.set_cell(module=1, cell=4, mV=3500)   # min
        bms_emulator.set_cell(module=3, cell=10, mV=4100)  # max

        # Wait for at least one telemetry cycle after the change
        time.sleep(0.7)

        frame = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
        assert frame is not None, "No 0x4A0 telemetry seen"
        s = M.decode_telem_status(frame.data)
        assert s["min_cell_mV"] == 3500, (
            f"min_cell_mV in 0x4A0 = {s['min_cell_mV']} (expected 3500). "
            "Either BMS RX parsing flipped endianness or the wrong cell index "
            "was treated as min."
        )
        assert s["max_cell_mV"] == 4100, (
            f"max_cell_mV in 0x4A0 = {s['max_cell_mV']} (expected 4100)."
        )


# ---------------------------------------------------------------------------
# HIL-033: BMS temperature parsing round-trips through firmware
# ---------------------------------------------------------------------------

class TestBmsTempParsing:
    def test_min_max_temps_reflect_emulator(self, bms_emulator, observe_acu):
        bms_emulator.set_all_temps(25)
        bms_emulator.set_temp(module=0, sensor=0,  C=-5)    # min
        bms_emulator.set_temp(module=4, sensor=10, C=55)    # max

        time.sleep(1.0)

        frame = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert frame is not None, "No 0x4A2 telemetry seen"
        t = M.decode_telem_temps(frame.data)
        assert t["min_tempC"] == -5, f"min_tempC = {t['min_tempC']} (expected -5)"
        assert t["max_tempC"] == 55, f"max_tempC = {t['max_tempC']} (expected 55)"


# ---------------------------------------------------------------------------
# HIL-035: ACU 0x100 DC bus parsing
# ---------------------------------------------------------------------------

class TestAcuDcBusParsing:
    def test_dc_bus_reflected_in_telemetry(self, acu, observe_acu):
        acu.send_dc_bus_v(300)
        time.sleep(0.7)

        frame = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert frame is not None, "No 0x4A2 telemetry seen"
        t = M.decode_telem_temps(frame.data)
        assert t["dc_bus_V"] == 300, (
            f"dc_bus_V in 0x4A2 = {t['dc_bus_V']} (expected 300). "
            "Either VCU 0x100 decode flipped endianness or the value wasn't "
            "latched into VehicleState."
        )


# ---------------------------------------------------------------------------
# HIL-036, HIL-037: start button + charger-detect via state observation
# (covered by Block C — re-asserted here as Block D's "frame parsing" angle)
# ---------------------------------------------------------------------------

class TestAcuStartButton:
    def test_start_button_triggers_precharge(self, acu, wait_for_state):
        acu.send_start_button(pressed=True)
        wait_for_state(M.FsmState.PRECHARGE)


class TestAcuChargerDetect:
    def test_charger_detect_triggers_charge(self, acu, wait_for_state):
        acu.send_charger_detect()
        wait_for_state(M.FsmState.CHARGE)


# ---------------------------------------------------------------------------
# HIL-038: ACU TX min cell V cadence in Run state
# ---------------------------------------------------------------------------

class TestAcuMinCellVCadence:
    def test_min_cell_v_500ms_in_run(self, acu, observe_acu, bms_emulator,
                                     wait_for_state, ams_profile, dc_bus_loop=None):
        # Get to Run via the C-block sequence
        acu.send_start_button(pressed=True)
        wait_for_state(M.FsmState.PRECHARGE)
        # Send DC bus continuously (no fixture here — inline)
        import threading
        stop = threading.Event()
        def loop():
            while not stop.is_set():
                acu.send_dc_bus_v(360); time.sleep(0.02)
        t = threading.Thread(target=loop, daemon=True); t.start()
        try:
            wait_for_state(M.FsmState.TRANSITION)
            wait_for_state(M.FsmState.RUN, timeout_ms=M.TRANSITION_HOLD_MS + 200)

            observe_acu.clear()
            time.sleep(2.5)   # 5 expected cycles

            period = observe_acu.mean_period_ms(M.ID_MIN_CELL_V_TX, extended=True)
            assert period is not None, "No 0x12C ext TX seen in Run state"
            target = ams_profile["tx_min_cell_v_period_ms"]
            jitter = ams_profile["tx_min_cell_v_jitter_ms"]
            assert abs(period - target) < jitter, (
                f"0x12C ext TX cadence = {period:.0f} ms, expected {target} ± {jitter}"
            )
        finally:
            stop.set(); t.join(timeout=1.0)


# ---------------------------------------------------------------------------
# HIL-040: charger-suppress — 0x12C ext and BMS volt polls stop in Charge
# ---------------------------------------------------------------------------

class TestChargerSuppress:
    def test_charging_suppresses_0x12c_and_bms_volt_polls(self, acu,
                                                          observe_acu,
                                                          observe_bms,
                                                          wait_for_state):
        acu.send_charger_detect()
        wait_for_state(M.FsmState.CHARGE)

        # Allow steady-state for 1 s, then sniff 2 s and assert silence
        time.sleep(1.0)
        observe_acu.clear()
        observe_bms.clear()
        time.sleep(2.0)

        assert observe_acu.count(M.ID_MIN_CELL_V_TX, extended=True) == 0, (
            "0x12C ext TX should be suppressed while charging."
        )
        # No voltage polls from the firmware while charging
        for m in range(M.NUM_BMS_MODULES):
            assert observe_bms.count(M.bms_voltage_poll_id(m)) == 0, (
                f"Module {m} voltage poll TX should be suppressed while charging."
            )
