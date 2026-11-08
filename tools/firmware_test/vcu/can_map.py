"""
VCU / ECU CAN frame definitions for the HIL bench (IFS08-CE-ECU#62 (ecu-rework)).

Sources of truth:
  - VCU TX:   isc-fs/IFS08-CE-ECU Core/Inc/can/messages/*.def (the CAN DSL that
              generates docs/dbc/ecu.dbc) — 0x100 heartbeat (STANDARD 11-bit, LE),
              0x700-0x705 + 0x7E1 pit-diag. Multi-byte pit-diag fields are BE;
              layouts verified vs ecu-rework (24e93a7).
  - Inverter: NX0001-STS04_A16.dbc (node EMC). VCU→inv 0x360/0x362,
              inv→VCU 0x461-0x466. Every NX frame carries AUTOSAR E2E
              Profile-1 (CRC=byte0, rolling CNT=bits8-11).

Bus assignment (kernel netdev ↔ ECU FDCAN, operating-model invariant #3):
    FDCAN1 INV → kernel `can0`  (PCB CAN3, U21)
    FDCAN2 ACU → kernel `can2`  (PCB CAN1, U17) — also the flash bus
"""
from __future__ import annotations

from enum import IntEnum

# -- Bus assignment ---------------------------------------------------
INV_KERNEL = "can0"
ACU_KERNEL = "can2"

# -- VCU TX (observe) -------------------------------------------------
ID_HEARTBEAT       = 0x100   # STANDARD 11-bit, 2 bytes (LE u16 dc_bus volts)
HEARTBEAT_EXTENDED = False   # STANDARD 11-bit id (ecu-rework)

ID_PIT_ENABLE   = 0x7E0   # bench→VCU: 'DEADBEEF' enable / 0 disable
ID_PIT_ACK      = 0x7E1   # VCU→bench: 1 enabled / 0 disabled
ID_PIT_STATUS   = 0x700   # fsm/inv state, flags, torque, v_cell_min
ID_PIT_PEDALS   = 0x701   # APPS1/2 raw+%, brake raw
ID_PIT_INVERTER = 0x702   # inverter dc_bus, rpm, error
ID_PIT_FWINFO   = 0x703   # fw version + git hash
ID_PIT_HEALTH   = 0x704   # firmware health @1 Hz (DiagTask) — the IWDG instrument
ID_PIT_BRAKE    = 0x705   # brake pressure + %
ID_PIT_TEMPS    = 0x706   # inverter board/stage/motor temps (raw -50 = degC)

# -- uDV / DV drive mode (FDCAN2/ACU, #101 Block L) -------------------
ID_DV_TS_ACTIVE   = 0x504   # VCU->uDV: TS-active (1B bool @100ms; tracks AMS precharge fresh)
ID_DV_BRAKE_OVER  = 0x505   # VCU->uDV: brake > BrakeDvHardRaw (1B bool @100ms)
ID_DV_MOTOR_RPM   = 0x506   # VCU->uDV: mechanical rpm (s32 LE @10ms, = inverter erpm / 10 pole pairs)
ID_DV_R2D_CONFIRM = 0x511   # VCU->uDV: DV R2D confirm (1B, byte0!=0 while the DV latch is set)
ID_UDV_TORQUE     = 0x507   # uDV->VCU: torque cmd (s32 LE integer %, stale >100ms)
ID_UDV_R2D_REQ    = 0x510   # uDV->VCU: R2D request (1B bool, stale >200ms)

# -- Electronic Differential & Torque Vectoring -----------------------
ID_LWS_STANDARD           = 0x2B0   # Bosch Motorsport LWS (can2/ACU, F02U.V02.894-01): Angle LE s16, Speed u8, Status
ID_LWS_CONFIG             = 0x7C0   # Bosch LWS Config / Calibration
ID_XSENS_IMU              = 0x201   # Xsens MTi IMU (can2/ACU): Yaw rate BE s16, Lat accel BE s16
ID_DTI_SET_CURRENT_LEFT   = 0x021   # DTI Packet 0x01 Node 1: Set AC Current (BE s16 0.1 Apk)
ID_DTI_SET_CURRENT_RIGHT  = 0x022   # DTI Packet 0x01 Node 2: Set AC Current (BE s16 0.1 Apk)
ID_DTI_DRIVE_ENABLE_LEFT  = 0x181   # DTI Packet 0x0C Node 1: Drive Enable (byte0 bool)
ID_DTI_DRIVE_ENABLE_RIGHT = 0x182   # DTI Packet 0x0C Node 2: Drive Enable (byte0 bool)
ID_DTI_ERPM_LEFT          = 0x401   # DTI Packet 0x20 Node 1: Telemetry ERPM (BE s32), Duty, Vdc
ID_DTI_ERPM_RIGHT         = 0x402   # DTI Packet 0x20 Node 2: Telemetry ERPM (BE s32), Duty, Vdc

# -- Inverter (NX0001-STS04, node EMC, on INV/can0) -------------------
ID_INV_CMD    = 0x360   # EMC_RX_SETPOINT_1 (VCU→inv): App_State_Req, Flt_Clear
ID_INV_TORQUE = 0x362   # EMC_RX_SETPOINT_3 (VCU→inv): Torque_Nm_Req
ID_INV_STATE2 = 0x461   # EMC_TX_STATE_2 (inv→VCU): App_State_App = inv_state
ID_INV_RPM    = 0x463   # EMC_TX_STATE_4 (inv→VCU): EMachine_Speed_erpm (20-bit signed @bit44)
ID_INV_TEMPS  = 0x464   # EMC_TX_STATE_5 (inv→VCU): board/stage/motor temps (raw -50 = degC)
ID_INV_STATE7 = 0x466   # EMC_TX_STATE_7 (inv→VCU): DCBus_Voltage_V

# -- ACU (from AMS, on ACU/can2) -------------------------------------
ID_ACU_PRECHARGE = 0x020   # ok_precarga (VCU RX)
ID_AMS_STATUS    = 0x4A0   # AMS 0x4A0[0] FSM state


class VcuFsmState(IntEnum):
    """ecu-rework consolidated startup FSM (IFS08-CE-ECU#62). Numeric values
    are the firmware enum order, reported in pit-diag 0x700[0]."""
    WAIT_INV_VDC_CONFIG = 0
    PRECHARGE           = 1
    WAIT_START_BRAKE    = 2
    R2D_DELAY           = 3
    WAIT_INV_STANDBY    = 4
    ACTIVE              = 5
    AMS_ERROR           = 6

    @classmethod
    def name_of(cls, v: int) -> str:
        try:
            return cls(v).name
        except ValueError:
            return f"?{v}"


class InvState(IntEnum):
    """Inverter App_State_App reported on 0x461 (inv→VCU); >=10 = fault."""
    STANDBY    = 3
    READY      = 4
    TORQUE     = 6
    FAULT_SOFT = 10
    FAULT_HARD = 11


class InvMode(IntEnum):
    """ECU→inv command word on 0x360 (App_State_Req / EMC_RX_SETPOINT_1). The
    FSM walks Off→Ready→TorqueEnable; Fault on inverter fault. Emitted by the
    deferred inverter adapter (firmware TODO #10) — Block E gate (E2E-framed)."""
    OFF              = 0x01
    READY            = 0x04
    TORQUE_ENABLE    = 0x06
    HARD_FAULT_RESET = 0x0D   # inv_state 11 (hard fault) -> command reset
    FAULT            = 0x13   # inv_state 10 (soft fault)


class ResetCause(IntEnum):
    """pit-diag 0x704.reset_cause."""
    UNKNOWN = 0
    POR     = 1
    PIN     = 2
    SOFT    = 3
    IWDG    = 4
    WWDG    = 5
    LPWR    = 6   # LPWR / BOR


# pit-diag 0x700.flags bitmask
PIT_FLAG_EV_2_3       = 1 << 0   # brake+throttle implausibility (latched)
PIT_FLAG_T11_8_9      = 1 << 1   # APPS disagreement past the 100 ms window
PIT_FLAG_RTDS_ACTIVE  = 1 << 2
PIT_FLAG_OK_PRECHARGE = 1 << 3
PIT_FLAG_START_BUTTON = 1 << 4

# pit-diag 0x704.task_ran_mask bits (EcuTaskId) — a frozen bit = a stalled task.
# ECU >= 1.0.0 is a 5-task layout: TELEMETRY took bit3 and DIAG MOVED to bit4
# (pit_diag_health.def). Pre-1.0.0 firmware had DIAG at bit3 and no telemetry.
TASK_CONTROL   = 1 << 0
TASK_CAN_RX    = 1 << 1
TASK_CAN_TX    = 1 << 2
TASK_TELEMETRY = 1 << 3
TASK_DIAG      = 1 << 4
TASK_ALL       = (TASK_CONTROL | TASK_CAN_RX | TASK_CAN_TX
                  | TASK_TELEMETRY | TASK_DIAG)

# pit-diag 0x704.last_fault sentinels (0 = none)
LAST_FAULT = {
    0x00: "none",
    0xF1: "hardfault", 0xF2: "memmanage", 0xF3: "busfault", 0xF4: "usagefault",
    0xF5: "stack_overflow", 0xF6: "malloc_failed", 0xF7: "assert",
}


# -- LE bit-field helpers (DBC @1+ = Intel / little-endian) -----------
def _u(data: bytes, start: int, length: int) -> int:
    v = int.from_bytes(data, "little")
    return (v >> start) & ((1 << length) - 1)


def _s(data: bytes, start: int, length: int) -> int:
    raw = _u(data, start, length)
    if raw >= (1 << (length - 1)):
        raw -= (1 << length)
    return raw


# -- Decoders (observe) ----------------------------------------------
def decode_heartbeat(data: bytes) -> dict:
    return {"dc_bus_v": int.from_bytes(data[:2], "little")}


def decode_pit_status(data: bytes) -> dict:
    """0x700 (8 B): fsm_state, inv_state, flags bitmask, torque %, v_cell_min
    (BE16), torque_cmd (BE signed16). Short frame → fsm_state only."""
    if len(data) < 8:
        return {"fsm_state": data[0] if data else None}
    return {
        "fsm_state":     data[0],
        "inv_state":     data[1],
        "flags":         data[2],
        "torque_pct":    data[3],
        "v_cell_min_mV": int.from_bytes(data[4:6], "big"),
        "torque_cmd":    int.from_bytes(data[6:8], "big", signed=True),
    }


def decode_pit_pedals(data: bytes) -> dict:
    """0x701 (8 B): APPS1/2 raw (BE16) + brake raw (BE16) + APPS1/2 %."""
    return {
        "apps1_raw": int.from_bytes(data[0:2], "big"),
        "apps2_raw": int.from_bytes(data[2:4], "big"),
        "brake_raw": int.from_bytes(data[4:6], "big"),
        "apps1_pct": data[6],
        "apps2_pct": data[7],
    }


def decode_pit_inverter(data: bytes) -> dict:
    """0x702 (7 B): inverter dc_bus (BE16), rpm (BE signed32), error code."""
    return {
        "dc_bus_v":  int.from_bytes(data[0:2], "big"),
        "inv_rpm":   int.from_bytes(data[2:6], "big", signed=True),
        "inv_error": data[6],
    }


def decode_pit_fwinfo(data: bytes) -> dict:
    """0x703 (7 B): fw major/minor/patch + git-hash prefix (BE u32)."""
    return {
        "fw_version": (data[0], data[1], data[2]),
        "git_hash":   int.from_bytes(data[3:7], "big"),
    }


def decode_pit_health(data: bytes) -> dict:
    """0x704 (8 B): free_heap/min (BE16), task_ran_mask, reset_cause, uptime_s,
    last_fault. The IWDG instrument — @1 Hz from DiagTask, survives a
    ControlTask stall (see LAST_FAULT / ResetCause / TASK_* helpers).

    Bytes 4 and 5 are packed (pit_diag_health.def): byte4 = task liveness b0-b4
    + stub announce b5-b7; byte5 = reset_cause b0-b2, stub_brake b3,
    cal_status b4-b5, stub_torque_cap b6, boot_refused b7 (sticky). Reading
    either as a whole byte turns a stub/cal/refusal bit into a bogus task or
    reset cause."""
    b4, b5 = data[4], data[5]
    return {
        "free_heap":        int.from_bytes(data[0:2], "big"),
        "min_free_heap":    int.from_bytes(data[2:4], "big"),
        "task_ran_mask":    b4 & TASK_ALL,
        "stub_no_ams":      (b4 >> 5) & 1,
        "stub_no_inverter": (b4 >> 6) & 1,
        "stub_start":       (b4 >> 7) & 1,
        "reset_cause":      b5 & 0x07,
        "stub_brake":       (b5 >> 3) & 1,
        "cal_status":       (b5 >> 4) & 0x03,
        "stub_torque_cap":  (b5 >> 6) & 1,
        "boot_refused":     (b5 >> 7) & 1,
        "uptime_s":         data[6],
        "last_fault":       data[7],
    }


def decode_pit_brake(data: bytes) -> dict:
    """0x705 (3 B): brake pressure (BE16 ×0.1 bar) + brake %."""
    return {
        "brake_pressure_bar": int.from_bytes(data[0:2], "big") * 0.1,
        "brake_pct":          data[2],
    }


def decode_inv_cmd(data: bytes) -> dict:
    # EMC_RX_SETPOINT_1: App_State_Req @bit16 w7, Flt_Clear @bit23 w1
    return {"app_state_req": _u(data, 16, 7), "flt_clear": _u(data, 23, 1)}


def decode_inv_torque(data: bytes) -> dict:
    # EMC_RX_SETPOINT_3: Torque_Nm_Req @bit16 w16 signed
    return {"torque_nm": _s(data, 16, 16)}


# -- E2E AUTOSAR Profile-1 (SAE-J1850 CRC8, poly 0x1D, init/xor 0xFF) --
# CAVEAT: the per-message Data-ID seed AND whether the VCU validates E2E on
# RX are UNCONFIRMED. Verify against the inverter spec + VCU `can.c` before
# trusting injected 0x461/0x466 in Blocks C/D/E. (Observe paths — B/D/F —
# don't need this.) If the VCU ignores E2E in HIL, any CRC/CNT will do.
def crc8_j1850(payload: bytes) -> int:
    crc = 0xFF
    for b in payload:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc ^ 0xFF


def _e2e_frame(dlc: int, cnt: int, data_id: int, sig_writer) -> bytes:
    """E2E-P1 frame: byte0=CRC, byte1 low-nibble=CNT (bits8-11), high-nibble
    ZERO. `sig_writer(bytearray)` places the payload signal(s)."""
    buf = bytearray(dlc)
    buf[1] = cnt & 0x0F
    sig_writer(buf)
    buf[0] = crc8_j1850(bytes([data_id & 0xFF]) + bytes(buf[1:]))
    return bytes(buf)


def encode_inv_state2(inv_state: int, cnt: int, data_id: int = 0x461) -> bytes:
    """EMC_TX_STATE_2 (0x461, 7 B): inv_state in byte4. Firmware reads
    `data[4] & 0x0F` (can.c:93) with NO E2E validation — any CRC/CNT accepted."""
    def w(buf):
        buf[4] = inv_state & 0x0F
    return _e2e_frame(7, cnt, data_id, w)


def encode_inv_state7(dc_bus_v: int, cnt: int, data_id: int = 0x466) -> bytes:
    """EMC_TX_STATE_7 (0x466, 6 B): DC-bus @bytes2-3. Firmware reads
    `read_u16_le(&data[2])` and sets inv_vdc_ready on *any* receipt (can.c:134),
    so even a 0 V frame opens the WAIT_INV_VDC_CONFIG gate."""
    def w(buf):
        v = dc_bus_v & 0xFFFF
        buf[2] = v & 0xFF
        buf[3] = (v >> 8) & 0xFF
    return _e2e_frame(6, cnt, data_id, w)


def encode_inv_rpm(rpm: int) -> bytes:
    """EMC_TX_STATE_4 (0x463, 8 B): EMachine_Speed_erpm, 20-bit SIGNED little-endian
    at frame bit44 (LSB = byte5 bit4). Firmware decode_inv_rpm reads d5[4:7]|d6|d7
    and sign-extends bit19, with no E2E check (vehicle_service.cpp:46) -- bytes 0-4
    are don't-care, so this is a plain (non-E2E) frame."""
    raw = int(rpm) & 0xFFFFF                          # 20-bit two's complement
    buf = bytearray(8)
    buf[5] = (raw & 0x0F) << 4
    buf[6] = (raw >> 4) & 0xFF
    buf[7] = (raw >> 12) & 0xFF
    return bytes(buf)


def encode_inv_temps(board: int, pwrstg: int, motor1: int, motor2: int) -> bytes:
    """EMC_TX_STATE_5 (0x464, 4 B): four inverter temps, each a raw byte where the
    DBC offset is -50 (raw = degC + 50). Firmware copies bytes 0-3 verbatim
    (vehicle_service.cpp:84)."""
    return bytes((
        (int(board)  + 50) & 0xFF,
        (int(pwrstg) + 50) & 0xFF,
        (int(motor1) + 50) & 0xFF,
        (int(motor2) + 50) & 0xFF,
    ))


def decode_pit_temps(data: bytes) -> dict:
    """0x706 (4 B): inverter board/stage/motor1/motor2 temps, each uint8 with a -50
    offset -> degC (PitDiag_inverter_temps.def)."""
    return {
        "temp_board_degC":  data[0] - 50,
        "temp_pwrstg_degC": data[1] - 50,
        "temp_motor1_degC": data[2] - 50,
        "temp_motor2_degC": data[3] - 50,
    }


# -- uDV / DV drive mode (#101 Block L) -------------------------------
def encode_udv_torque(pct: int) -> bytes:
    """0x507 UDV_torque_cmd (4 B): integer torque percent, s32 LE. Negative/garbage
    is a valid input -- the firmware conditioner clamps <0 -> 0, >100 -> 100."""
    return int(pct).to_bytes(4, "little", signed=True)


def encode_udv_r2d(request: int) -> bytes:
    """0x510 UDV_r2d_request (1 B): byte0 = R2D-request bool."""
    return bytes([int(request) & 0xFF])


def decode_ts_active(data: bytes) -> dict:
    """0x504 VCU_ts_active (1 B): byte0 = ts_active (fresh AMS ok_precharge view)."""
    return {"ts_active": data[0]}


def decode_brake_over(data: bytes) -> dict:
    """0x505 VCU_brake_over_limit (1 B): byte0 = brake_raw > BrakeDvHardRaw (2500)."""
    return {"brake_over_limit": data[0]}


def decode_motor_rpm(data: bytes) -> dict:
    """0x506 VCU_motor_rpm (4 B): mechanical rpm, s32 LE (= inverter erpm / 10)."""
    return {"motor_rpm": int.from_bytes(data[0:4], "little", signed=True)}


def decode_r2d_confirm(data: bytes) -> dict:
    """0x511 VCU_r2d_confirm (1 B): byte0 != 0 while the DV latch is set."""
    return {"r2d_confirm": data[0]}


# DV torque map (inverter.cpp:11) -- the expected 0x362 for a % command.
DV_TORQUE_DEADBAND_PCT = 10


def dv_conditioned_pct(cmd: int) -> int:
    """0x507 conditioner (vehicle_service.cpp): <0 -> 0, >100 -> 100, else cmd."""
    return 0 if cmd < 0 else (100 if cmd > 100 else int(cmd))


def dv_torque_nm(pct: int) -> int:
    """Expected 0x362 Torque_Nm_Req (s16) for a *conditioned* DV percent -- matches the
    firmware map: <10% -> 0; else -(pct*240//90 - 2400//90). e.g. 40 -> -80, 100 -> -240."""
    if pct < DV_TORQUE_DEADBAND_PCT:
        return 0
    return -(pct * 240 // 90 - 2400 // 90)


# -- Electronic Differential Codecs -----------------------------------
def encode_lws_standard(angle_deg: float, speed_deg_s: float = 0.0,
                        ok: bool = True, cal: bool = True, trim: bool = True) -> bytes:
    """Bosch Motorsport LWS_Standard (0x2B0, 5 B, Intel / Little-Endian):
    Bytes 0-1: Angle signed int16 LE, 0.1 deg/LSB (+/-780 deg, 0x7FFF = uncalibrated)
    Byte 2:    Speed uint8, 4.0 deg/s/LSB (0 to 1,016 deg/s)
    Byte 3:    bits 0: OK, 1: CAL, 2: TRIM (0x07 = calibrated & valid)
    Byte 4:    Reserved (0x00)
    """
    raw_angle = 0x7FFF if not cal else int(round(angle_deg / 0.1)) & 0xFFFF
    raw_speed = 0xFF if (not ok or not trim) else min(254, max(0, int(round(speed_deg_s / 4.0))))
    status = (1 if ok else 0) | ((1 if cal else 0) << 1) | ((1 if trim else 0) << 2)
    return bytes([
        raw_angle & 0xFF,
        (raw_angle >> 8) & 0xFF,
        raw_speed & 0xFF,
        status & 0xFF,
        0x00,
    ])


def decode_lws_standard(data: bytes) -> dict:
    raw_angle = int.from_bytes(data[0:2], "little", signed=True)
    raw_speed = data[2] if len(data) > 2 else 0
    status = data[3] if len(data) > 3 else 0
    ok = bool(status & 0x01)
    cal = bool(status & 0x02)
    trim = bool(status & 0x04)
    valid = ok and cal and trim and (raw_angle != 0x7FFF)
    return {
        "angle_deg": raw_angle * 0.1 if valid else None,
        "speed_deg_s": raw_speed * 4.0,
        "ok": ok,
        "cal": cal,
        "trim": trim,
        "valid": valid,
    }


def encode_xsens_imu(yaw_rate_rads: float = 0.0, lateral_accel_mps2: float = 0.0,
                     longitudinal_accel_mps2: float = 0.0) -> bytes:
    """Xsens MTi IMU (0x201, 8 B, Big-Endian):
    Bytes 0-1: Yaw Rate signed int16 BE, 0.001 rad/s/LSB
    Bytes 2-3: Lateral Acceleration a_y signed int16 BE, 0.01 m/s^2/LSB
    Bytes 4-5: Longitudinal Acceleration a_x signed int16 BE, 0.01 m/s^2/LSB
    Bytes 6-7: Reserved (0x00)
    """
    raw_yaw = int(round(yaw_rate_rads / 0.001)) & 0xFFFF
    raw_ay  = int(round(lateral_accel_mps2 / 0.01)) & 0xFFFF
    raw_ax  = int(round(longitudinal_accel_mps2 / 0.01)) & 0xFFFF
    return bytes([
        (raw_yaw >> 8) & 0xFF, raw_yaw & 0xFF,
        (raw_ay >> 8) & 0xFF,  raw_ay & 0xFF,
        (raw_ax >> 8) & 0xFF,  raw_ax & 0xFF,
        0x00, 0x00,
    ])


def decode_xsens_imu(data: bytes) -> dict:
    raw_yaw = int.from_bytes(data[0:2], "big", signed=True)
    raw_ay  = int.from_bytes(data[2:4], "big", signed=True)
    raw_ax  = int.from_bytes(data[4:6], "big", signed=True) if len(data) >= 6 else 0
    return {
        "yaw_rate_rads": raw_yaw * 0.001,
        "lateral_accel_mps2": raw_ay * 0.01,
        "longitudinal_accel_mps2": raw_ax * 0.01,
    }


def decode_dti_set_current(data: bytes) -> dict:
    """DTI Inverter Packet 0x01 Set Current: signed int16 BE @ bytes 0-1, scaled by 10 (0.1 Apk/LSB)."""
    raw = int.from_bytes(data[0:2], "big", signed=True)
    current_apk = raw * 0.1
    # Dual DTI F-MOT Kt = 0.696 Nm/A peak
    torque_nm = current_apk * 0.696
    return {
        "current_apk": current_apk,
        "torque_nm":   torque_nm,
    }


def decode_dti_drive_enable(data: bytes) -> dict:
    """DTI Inverter Packet 0x0C Drive Enable: byte 0 (0 or 1)."""
    return {"drive_enabled": bool(data[0])}


def encode_dti_erpm(erpm: int, duty_pct: float = 0.0, input_voltage_v: float = 500.0) -> bytes:
    """DTI Inverter Packet 0x20 Telemetry:
    Bytes 0-3: ERPM signed int32 BE
    Bytes 4-5: Duty cycle signed int16 BE * 10
    Bytes 6-7: Input voltage signed int16 BE (Volts)
    """
    raw_erpm = int(erpm) & 0xFFFFFFFF
    raw_duty = int(round(duty_pct * 10.0)) & 0xFFFF
    raw_vdc  = int(round(input_voltage_v)) & 0xFFFF
    return bytes([
        (raw_erpm >> 24) & 0xFF, (raw_erpm >> 16) & 0xFF, (raw_erpm >> 8) & 0xFF, raw_erpm & 0xFF,
        (raw_duty >> 8) & 0xFF,  raw_duty & 0xFF,
        (raw_vdc >> 8) & 0xFF,   raw_vdc & 0xFF,
    ])
