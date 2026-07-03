"""
Block J (telemetry decode) — inverter rpm + temps mirrored to pit-diag
(IFS08-CE-ECU#94 J-001/J-002).

The ECU decodes the NX inverter's speed and temperatures off the INV bus and mirrors
them into the pit-diag stream (control_task.cpp:137-138):
  0x463 EMachine_Speed_erpm (20-bit signed @bit44) -> 0x702.inv_rpm (signed 32-bit)
  0x464 four temps (raw -50 = degC)                -> 0x706 board/stage/motor1/motor2
Both were hardcoded/absent before #86 (rpm was a literal 0; 0x706 didn't exist). This
asserts the decode end-to-end over CAN: inject the inverter frame, read it back from
the gated pit-diag stream.
"""
from __future__ import annotations

import time

import pytest

from tools.firmware_test.vcu import can_map as M


def _last_decoded(observe_acu, can_id, decoder, timeout_s=1.5):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        f = observe_acu.last(can_id, extended=False)
        if f is not None:
            return decoder(f.data)
        time.sleep(0.05)
    return None


class TestJ001RpmDecode:
    @pytest.mark.parametrize("rpm", [0, 3500, 12000, -4200])
    def test_j001_inverter_rpm_mirrors_to_pitdiag(self, fresh_boot, inv_heartbeat,
                                                  pit_diag, observe_acu, rpm):
        """J-001: inject 0x463 EMachine_Speed_erpm -> 0x702.inv_rpm matches (signed)."""
        inv_heartbeat["set_rpm"](rpm)
        time.sleep(0.4)   # let a fresh 0x702 (100 ms cadence) carry the new value
        dec = _last_decoded(observe_acu, M.ID_PIT_INVERTER, M.decode_pit_inverter)
        assert dec is not None, "no 0x702 pit-diag inverter frame (is pit-diag enabled?)"
        assert dec["inv_rpm"] == rpm, \
            f"0x702 inv_rpm {dec['inv_rpm']} != injected 0x463 rpm {rpm}"


class TestJ002TempsDecode:
    def test_j002_inverter_temps_mirror_to_pitdiag(self, fresh_boot, inv_heartbeat,
                                                   pit_diag, observe_acu):
        """J-002: inject 0x464 four temps -> 0x706 board/stage/motor1/motor2 (degC),
        incl. a sub-zero motor temp to exercise the -50 offset."""
        board, pwrstg, motor1, motor2 = 35, 48, 60, -12
        inv_heartbeat["set_temps"](board, pwrstg, motor1, motor2)
        time.sleep(0.4)
        dec = _last_decoded(observe_acu, M.ID_PIT_TEMPS, M.decode_pit_temps)
        assert dec is not None, \
            "no 0x706 pit-diag temps frame (pit-diag enabled? inverter-temps #86 merged?)"
        assert dec["temp_board_degC"] == board,  f"board {dec['temp_board_degC']} != {board}"
        assert dec["temp_pwrstg_degC"] == pwrstg, f"pwrstg {dec['temp_pwrstg_degC']} != {pwrstg}"
        assert dec["temp_motor1_degC"] == motor1, f"motor1 {dec['temp_motor1_degC']} != {motor1}"
        assert dec["temp_motor2_degC"] == motor2, f"motor2 {dec['temp_motor2_degC']} != {motor2}"
