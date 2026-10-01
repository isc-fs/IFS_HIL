"""Guards on the ECU 0x704 PitDiag_health decoder.

Bytes 4 and 5 of 0x704 are packed bit-fields (IFS08-CE-ECU
Core/Inc/can/messages/pit_diag_health.def). The decoder used to read each as a
whole byte, with DIAG at bit3 -- the pre-1.0.0 four-task layout. On ECU >= 1.0.0
bit3 is TelemetryTask and DIAG is bit4, so I-003 checked the wrong task, and a
set cal_status / stub / boot_refused bit read as a bogus reset_cause.
"""
from tools.firmware_test.vcu import can_map as M


def _frame(b4, b5):
    return bytes([0x12, 0x34, 0x10, 0x00, b4, b5, 42, 0])


def test_task_bits_follow_the_five_task_layout():
    assert M.TASK_TELEMETRY == 1 << 3
    assert M.TASK_DIAG == 1 << 4
    assert M.TASK_ALL == 0x1F


def test_stub_announce_does_not_leak_into_task_mask():
    h = M.decode_pit_health(_frame(0b1110_0000 | M.TASK_DIAG, 0))
    assert h["task_ran_mask"] == M.TASK_DIAG
    assert (h["stub_no_ams"], h["stub_no_inverter"], h["stub_start"]) == (1, 1, 1)


def test_reset_cause_is_three_bits():
    # IWDG, plus stub_brake, cal_status=2, stub_torque_cap and boot_refused set
    b5 = M.ResetCause.IWDG | (1 << 3) | (2 << 4) | (1 << 6) | (1 << 7)
    h = M.decode_pit_health(_frame(0, b5))
    assert h["reset_cause"] == M.ResetCause.IWDG
    assert h["stub_brake"] == 1
    assert h["cal_status"] == 2
    assert h["stub_torque_cap"] == 1
    assert h["boot_refused"] == 1


def test_unpacked_fields():
    h = M.decode_pit_health(_frame(M.TASK_ALL, M.ResetCause.POR))
    assert h["free_heap"] == 0x1234
    assert h["min_free_heap"] == 0x1000
    assert h["task_ran_mask"] == M.TASK_ALL
    assert h["uptime_s"] == 42
    assert h["last_fault"] == 0
