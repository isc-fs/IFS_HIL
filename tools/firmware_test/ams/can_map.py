"""
AMS CAN frame definitions.

Source of truth: `isc-fs/IFS08-CE-AMS/docs/CAN_MAP.md`. Keep these in sync
when the firmware's `Core/Inc/app/ams_config.hpp` changes.

Bus assignment on the IFS08_HIL bench (kernel netdev ↔ firmware bus):

    FDCAN1 (vehicle / ACU)  →  kernel `can0`  (PCB CAN3, U21)
    FDCAN2 (BMS + BL)       →  kernel `can2`  (PCB CAN1, U17, also visible on can1
                                              because of the bench can1↔can2 tie)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Bus assignment (kernel-side names)
# ---------------------------------------------------------------------------
FDCAN1_KERNEL = "can0"   # ACU bus
FDCAN2_KERNEL = "can2"   # BMS + bootloader bus

# ---------------------------------------------------------------------------
# Module addressing — BMS slaves (5 modules)
# ---------------------------------------------------------------------------
# CANID(m) = 0x12C + m * 0x1E
#
#   voltage poll TX (AMS→slave):  CANID(m)
#   voltage resp RX (slave→AMS):  CANID(m) + 1 .. CANID(m) + 5
#   temp poll    TX:              CANID(m) + 20
#   temp resp    RX:              CANID(m) + 21 .. CANID(m) + 25
#
NUM_BMS_MODULES = 5
BMS_BASE_CANID = 0x12C
BMS_MODULE_STRIDE = 0x1E


def bms_canid(module: int) -> int:
    """Module-`module` base CANID. Module index 0..4."""
    if not 0 <= module < NUM_BMS_MODULES:
        raise ValueError(f"module must be 0..{NUM_BMS_MODULES-1}, got {module}")
    return BMS_BASE_CANID + module * BMS_MODULE_STRIDE


def bms_voltage_poll_id(module: int) -> int:
    return bms_canid(module)


def bms_voltage_resp_id(module: int, frame_idx: int) -> int:
    """5 voltage-response frames per module, frame_idx in 0..4."""
    if not 0 <= frame_idx < 5:
        raise ValueError(f"frame_idx must be 0..4, got {frame_idx}")
    return bms_canid(module) + 1 + frame_idx


def bms_temp_poll_id(module: int) -> int:
    return bms_canid(module) + 20


def bms_temp_resp_id(module: int, frame_idx: int) -> int:
    """5 temperature-response frames per module, frame_idx in 0..4."""
    if not 0 <= frame_idx < 5:
        raise ValueError(f"frame_idx must be 0..4, got {frame_idx}")
    return bms_canid(module) + 21 + frame_idx


# Convenience lookups
BMS_VOLTAGE_POLL_IDS = [bms_voltage_poll_id(m) for m in range(NUM_BMS_MODULES)]
BMS_TEMP_POLL_IDS    = [bms_temp_poll_id(m)    for m in range(NUM_BMS_MODULES)]

# Cells per BMS module (per CAN_MAP.md)
CELLS_PER_MODULE = 19
TEMPS_PER_MODULE = 38

# Frame-to-cell mapping (4 cells per frame, last has 3)
#   frame 0 (CANID+1):  cells 0..3
#   frame 1 (CANID+2):  cells 4..7
#   frame 2 (CANID+3):  cells 8..11
#   frame 3 (CANID+4):  cells 12..15
#   frame 4 (CANID+5):  cells 16..18  (bytes 6..7 unused)
CELLS_PER_FRAME = 4

# Frame-to-temp mapping (8 sensors per frame, last has 6)
TEMPS_PER_FRAME = 8

# Cadence (per CAN_MAP.md)
BMS_VOLTAGE_POLL_PERIOD_MS = 250
BMS_TEMP_POLL_PERIOD_MS    = 500
BMS_VOLTAGE_STALE_MS       = 1500  # legacy; firmware enforces
BMS_TEMP_STALE_MS          = 1000  # refactor: enforced

# ---------------------------------------------------------------------------
# ACU bus (FDCAN1) — RX from vehicle to AMS
# ---------------------------------------------------------------------------
ID_DC_BUS_VOLTAGE   = 0x100         # extended, DLC 2, little-endian volts
ID_START_BUTTON     = 0x600         # standard, DLC 1, byte 0 = 0/1
ID_CHARGER_DETECT   = 0x18FF50E7    # extended (29-bit), any payload

# ---------------------------------------------------------------------------
# ACU bus (FDCAN1) — TX from AMS to vehicle
# ---------------------------------------------------------------------------
ID_MIN_CELL_V_TX    = 0x12C         # extended, DLC 2, big-endian mV, 500 ms
                                    # suppressed when charging
ID_AMS_STATE_REPLY  = 0x20          # extended, DLC 1, state code
ID_CURRENT_TX       = 0x450         # standard, DLC 2, byte 1 = amps
ID_CURRENT_WARN     = 0x500         # 80..100% C_MAX
ID_CURRENT_OVER     = 0x501
ID_CURRENT_NORMAL   = 0x502

# Cadences for verifying TX (per CAN_MAP.md)
TX_MIN_CELL_V_PERIOD_MS = 500
TX_CURRENT_PERIOD_MS    = 250

# AMS state byte encoding for the legacy 0x20 reply (kept for reference;
# new code should prefer FsmState from the 0x4A0 telemetry frame).
STATE_CPU_POWER         = 0x00
STATE_CPU_PRECHARGE     = 0x01
STATE_CPU_DISCONNECTED  = 0x02
STATE_CPU_ERROR         = 0x03
STATE_CPU_CHARGING      = 0x04

# ---------------------------------------------------------------------------
# AMS telemetry frames (FDCAN1, replaces UART debug path)
# All standard ID, classic CAN, DLC 8, 500 ms cadence.
# Source of truth: Core/Inc/app/telemetry_encoders.hpp.
# ---------------------------------------------------------------------------
ID_TELEM_STATUS  = 0x4A0   # FSM state, AMS_OK, module mask, min/max cell V
ID_TELEM_PACK    = 0x4A1   # pack mV (u32 LE), filtered mA (i32 LE)
ID_TELEM_TEMPS   = 0x4A2   # min/max/avg tempC, dc_bus_V, heartbeat

TX_TELEM_PERIOD_MS = 500


# FSM state enum, matches Core/Inc/app/state_machine.hpp `ams::fsm::State`.
class FsmState:
    START      = 0
    PRECHARGE  = 1
    TRANSITION = 2
    RUN        = 3
    CHARGE     = 4
    ERROR      = 5

    _NAMES = {0: "Start", 1: "Precharge", 2: "Transition",
              3: "Run",   4: "Charge",    5: "Error"}

    @classmethod
    def name(cls, v: int) -> str:
        return cls._NAMES.get(int(v), f"0x{int(v):02X}")


# ---------------------------------------------------------------------------
# Telemetry decoders. Each takes an 8-byte payload and returns a dict.
# Layouts mirror telemetry_encoders.hpp byte-for-byte.
# ---------------------------------------------------------------------------

def decode_telem_status(data: bytes) -> dict:
    """Decode 0x4A0 payload.
    Layout:
      byte 0     FSM state (FsmState)
      byte 1     AMS_OK GPIO read-back (0/1)
      byte 2     bms.module_online_mask (low byte)
      byte 3     reserved
      bytes 4-5  min_cell_mV (big-endian uint16)
      bytes 6-7  max_cell_mV (big-endian uint16)
    """
    if len(data) < 8:
        raise ValueError(f"0x4A0 needs 8 bytes, got {len(data)}")
    return {
        "state":              data[0],
        "state_name":         FsmState.name(data[0]),
        "ams_ok":             bool(data[1]),
        "module_online_mask": data[2],
        "min_cell_mV":        (data[4] << 8) | data[5],
        "max_cell_mV":        (data[6] << 8) | data[7],
    }


def decode_telem_pack(data: bytes) -> dict:
    """Decode 0x4A1 payload.
    Layout:
      bytes 0-3  pack_voltage_mV (little-endian uint32, mV)
      bytes 4-7  filtered_mA     (little-endian int32, + discharge / - charge)
    """
    if len(data) < 8:
        raise ValueError(f"0x4A1 needs 8 bytes, got {len(data)}")
    pack_mV = int.from_bytes(data[0:4], "little", signed=False)
    mA      = int.from_bytes(data[4:8], "little", signed=True)
    return {"pack_voltage_mV": pack_mV, "filtered_mA": mA}


class ModeLocked:
    """Charger-mode lock captured at Start->Precharge per
    `isc-fs/IFS08-CE-AMS#187`. Lives in `0x4A2[5]` bits 3..2."""
    UNDECIDED = 0
    CAR       = 1
    CHARGER   = 2

    _NAMES = {0: "Undecided", 1: "Car", 2: "Charger"}

    @classmethod
    def name(cls, v: int) -> str:
        return cls._NAMES.get(int(v), f"0x{int(v):02X}")


def decode_cockpit_byte(b: int) -> dict:
    """Decode `0x4A2[5]` per `isc-fs/IFS08-CE-AMS#190` (HIL_STUB build).
    Layout:
      bit 7      sentinel (always 1; if low the byte is the old `reserved=0`)
      bits 3..2  mode_locked (see ModeLocked)
      bit 1      TSMS (PF9 read-back)
      bit 0      DASH_CHG (PF10 read-back)

    Returns `valid=False` if the sentinel isn't set (flight build, where
    `0x4A2[5]` is `0x00`)."""
    if (b & 0x80) == 0:
        return {"valid": False, "raw": b}
    return {
        "valid":       True,
        "raw":         b,
        "mode_locked": (b >> 2) & 0x03,
        "tsms":        bool(b & 0x02),
        "dash_chg":    bool(b & 0x01),
    }


def decode_telem_temps(data: bytes) -> dict:
    """Decode 0x4A2 payload.

    Flight layout (`-DAMS_BMS_HIL_STUB` not set):
      byte 0     min_tempC (int8)
      byte 1     max_tempC (int8)
      byte 2     avg_tempC (int8)
      bytes 3-4  dc_bus_V (little-endian uint16, volts)
      byte 5     reserved (zero)
      byte 6     tx_fail_count_lo
      byte 7     heartbeat counter (0..255, wraps)

    HIL_STUB layout (`-DAMS_BMS_HIL_STUB=1`):
      bytes 0-2  unchanged
      byte 3     bms_poll_task_state (0xA0..0xA4 / 0xAF / 0xFF)
      byte 4     acu_rx_total_lo
      byte 5     cockpit byte (see `decode_cockpit_byte`)
      byte 6     tx_fail_count_lo
      byte 7     heartbeat counter

    All raw bytes 3..6 are exposed as `byte3..byte6` so callers can pick
    the right interpretation for their build. `cockpit` is the decoded
    HIL byte; `dc_bus_V` is the decoded flight value -- check
    `cockpit["valid"]` to know which build you're looking at.
    """
    if len(data) < 8:
        raise ValueError(f"0x4A2 needs 8 bytes, got {len(data)}")
    def i8(b): return b - 256 if b > 127 else b
    return {
        "min_tempC":     i8(data[0]),
        "max_tempC":     i8(data[1]),
        "avg_tempC":     i8(data[2]),
        "dc_bus_V":      data[3] | (data[4] << 8),
        "byte3":         data[3],
        "byte4":         data[4],
        "byte5":         data[5],
        "byte6":         data[6],
        "tx_fail_count_lo": data[6],
        "cockpit":       decode_cockpit_byte(data[5]),
        "heartbeat":     data[7],
    }

# ---------------------------------------------------------------------------
# Bootloader trigger (FDCAN2)
# ---------------------------------------------------------------------------
ID_BOOT_TRIGGER     = 0x002         # standard, DLC 4
BOOT_TRIGGER_PAYLOAD = bytes([0xB0, 0x07, 0xAD, 0x11])
BOOT_TRIGGER_DLC    = 4

# ---------------------------------------------------------------------------
# Thresholds (from firmware ams_config.hpp / safety_predicates.hpp)
# ---------------------------------------------------------------------------
CELL_UV_MV          = 2800
CELL_OV_MV          = 4200
CELL_OT_C           = 60
PRECHARGE_MAX_MS    = 1500
TRANSITION_HOLD_MS  = 100
