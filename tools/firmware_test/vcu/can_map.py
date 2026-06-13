"""
VCU / ECU CAN frame definitions for the HIL bench (IFS08-CE-ECU#35).

Sources of truth:
  - VCU TX:   isc-fs/IFS08-CE-ECU/docs/dbc/ecu.dbc — 0x100 heartbeat (FDCAN
              EXTENDED id), 0x700-0x703 + 0x7E1 pit-diag.
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
ID_HEARTBEAT       = 0x100   # EXTENDED, 2 bytes (LE u16 dc_bus volts)
HEARTBEAT_EXTENDED = True

ID_PIT_ENABLE   = 0x7E0   # bench→VCU: 'DEADBEEF' enable / 0 disable
ID_PIT_ACK      = 0x7E1   # VCU→bench: 1 enabled / 0 disabled
ID_PIT_STATUS   = 0x700   # FSM state in byte[0]
ID_PIT_PEDALS   = 0x701
ID_PIT_INVERTER = 0x702
ID_PIT_FWINFO   = 0x703

# -- Inverter (NX0001-STS04, node EMC, on INV/can0) -------------------
ID_INV_CMD    = 0x360   # EMC_RX_SETPOINT_1 (VCU→inv): App_State_Req, Flt_Clear
ID_INV_TORQUE = 0x362   # EMC_RX_SETPOINT_3 (VCU→inv): Torque_Nm_Req
ID_INV_STATE2 = 0x461   # EMC_TX_STATE_2 (inv→VCU): App_State_App = inv_state
ID_INV_STATE7 = 0x466   # EMC_TX_STATE_7 (inv→VCU): DCBus_Voltage_V

# -- ACU (from AMS, on ACU/can2) -------------------------------------
ID_ACU_PRECHARGE = 0x020   # ok_precarga (VCU RX)
ID_AMS_STATUS    = 0x4A0   # AMS 0x4A0[0] FSM state


class VcuFsmState(IntEnum):
    """control.c startup FSM. Numeric values are the assumed declaration
    order — CONFIRM against the firmware control.h enum (pit-diag 0x700[0])."""
    WAIT_INV_VDC_CONFIG = 0
    BOOT                = 1
    WAIT_PRECHARGE_ACK  = 2
    WAIT_START_BRAKE    = 3
    R2D_DELAY           = 4
    WAIT_INV_STANDBY    = 5
    ACTIVE              = 6

    @classmethod
    def name_of(cls, v: int) -> str:
        try:
            return cls(v).name
        except ValueError:
            return f"?{v}"


class InvState(IntEnum):
    STANDBY    = 3
    READY      = 4
    TORQUE     = 6
    FAULT_SOFT = 10
    FAULT_HARD = 11


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
    return {"fsm_state": data[0] if data else None}


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
