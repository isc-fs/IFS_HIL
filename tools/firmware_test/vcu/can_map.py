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

# -- Inverter (NX0001-STS04, node EMC, on INV/can0) -------------------
ID_INV_CMD    = 0x360   # EMC_RX_SETPOINT_1 (VCU→inv): App_State_Req, Flt_Clear
ID_INV_TORQUE = 0x362   # EMC_RX_SETPOINT_3 (VCU→inv): Torque_Nm_Req
ID_INV_STATE2 = 0x461   # EMC_TX_STATE_2 (inv→VCU): App_State_App = inv_state
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
    OFF           = 0x01
    READY         = 0x04
    TORQUE_ENABLE = 0x06
    FAULT         = 0x13


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

# pit-diag 0x704.task_ran_mask bits (EcuTaskId) — a frozen bit = a stalled task
TASK_CONTROL = 1 << 0
TASK_CAN_RX  = 1 << 1
TASK_CAN_TX  = 1 << 2
TASK_DIAG    = 1 << 3

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
    ControlTask stall (see LAST_FAULT / ResetCause / TASK_* helpers)."""
    return {
        "free_heap":     int.from_bytes(data[0:2], "big"),
        "min_free_heap": int.from_bytes(data[2:4], "big"),
        "task_ran_mask": data[4],
        "reset_cause":   data[5],
        "uptime_s":      data[6],
        "last_fault":    data[7],
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
