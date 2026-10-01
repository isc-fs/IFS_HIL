"""
AMS CAN frame definitions.

Source of truth: `isc-fs/IFS08-CE-AMS/docs/CAN_MAP.md`. Keep these in sync
when the firmware's `Core/Inc/app/ams_config.hpp` changes.

Bus assignment on the IFS_HIL bench (kernel netdev ↔ firmware bus):

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
ID_DC_BUS_VOLTAGE   = 0x100         # STANDARD (11-bit), DLC 2, little-endian volts
                                    # Post AMS PR #236: FDCAN1 HW filter rejects
                                    # extended IDs, so this frame MUST be sent as
                                    # standard. A-012 verifies the reject path by
                                    # explicitly cansending extended form.
ID_BALANCE_OVERRIDE = 0x103         # STANDARD, DLC 4, operator -> AMS (#340).
                                    # Magic-gated: BALO suppress / BALX resume,
                                    # re-sent >=2 Hz; stale > 5 s reverts to auto.
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
ID_RELAY_STATUS  = 0x4A4   # AMS_relay_status (#395): contactor + AMS_OK ODR read-backs, ~100 ms

TX_TELEM_PERIOD_MS = 500
TX_RELAY_PERIOD_MS = 100   # 0x4A4 cadence (#395)


# ---------------------------------------------------------------------------
# Pit-diag stream (AMS PR #248 + #263 + #269). Toggled by a 0x7F0 cmd
# frame; AMS replies with 0x7F1 ACK then bursts the full 58-frame grid at
# 1 Hz. All standard ID, classic CAN, DLC 8 (except 0x7F0 = 4, 0x7F1 = 1).
# Source of truth: Core/Inc/app/pit_diag_emitter.hpp + ams_config.hpp.
# ---------------------------------------------------------------------------
ID_PIT_DIAG_CMD          = 0x7F0   # host -> AMS: enable/disable
ID_PIT_DIAG_ACK          = 0x7F1   # AMS -> host: one-shot {01|00} after change
ID_PIT_DIAG_CELL_BASE    = 0x680   # 24 frames: 4 cells/frame BE u16 mV
ID_PIT_DIAG_TEMP_BASE    = 0x6A0   # 25 frames: 8 NTCs/frame i8 degC
ID_PIT_DIAG_FSM_STATUS   = 0x6C0   # [0]fsm [1]mode [2]inputs(b0 DASH,b1 TSMS,b2 bal_override #340) [3]ams_ok [4..5]pec_err BE [6]fault_reason [7]detail
ID_PIT_DIAG_TIMING       = 0x6C1   # [0..1]volt_poll_ms BE [2..3]volt_poll_max BE [4..7]temp_sweep_mask LE
ID_PIT_DIAG_BAL_MASK_A   = 0x6C2
ID_PIT_DIAG_BAL_MASK_B   = 0x6C3
ID_PIT_DIAG_BOOT         = 0x6C4   # [0..3]jump_reason LE [4]app_init_progress [5..7]fdcan1_start_result
ID_PIT_DIAG_POST_MORTEM  = 0x6C5
ID_PIT_DIAG_FW_ID        = 0x6C6   # [0..2]semver [3..6]git_hash [7]bl_node_id
ID_PIT_DIAG_PEC_PER_IC_A = 0x6C7   # [0..7]saturating u8 PEC count per IC for chain index 0..7
ID_PIT_DIAG_PEC_PER_IC_B = 0x6C8   # [0..1]saturating u8 for chain 8..9, [2..7]reserved
ID_PIT_DIAG_FDCAN1_RECOVERY = 0x6C9   # [0..3]busoff_recovery_count LE u32 [4..7]acu_tx_fail LE u32 (#391/#392)

PIT_DIAG_ENABLE_MAGIC  = bytes([0xDE, 0xAD, 0xBE, 0xEF])
PIT_DIAG_DISABLE_MAGIC = bytes([0x00, 0x00, 0x00, 0x00])
PIT_DIAG_SCAN_PERIOD_MS = 1000     # 1 Hz scan when enabled
PIT_DIAG_PEC_SATURATION = 0xFF     # u8 saturation point for per-IC counts

# 0x103 balance-override magic (#340). Re-send >= 2 Hz to stay in effect;
# stale > BALANCE_OVERRIDE_FRESH_MS reverts the AMS to autonomous balancing.
BALANCE_OVERRIDE_SUPPRESS = bytes([0x42, 0x41, 0x4C, 0x4F])   # "BALO" -> suppress
BALANCE_OVERRIDE_RESUME   = bytes([0x42, 0x41, 0x4C, 0x58])   # "BALX" -> resume auto
BALANCE_OVERRIDE_FRESH_MS = 5000
# 0x6C0[2] inputs byte: bit0 DASH_CHG, bit1 TSMS, bit2 balance_override (#340).
BALANCE_OVERRIDE_BIT = 0x04


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


def decode_relay_status(data: bytes) -> dict:
    """Decode 0x4A4 AMS_relay_status byte 0 (#395). ODR read-backs of what
    the firmware drives the contactor coils + AMS_OK GPIO to:
      bit 0 air_negative, bit 1 air_positive, bit 2 precharge, bit 3 ams_ok.
    Bytes 1-7 are reserved (zero). This is the direct contactor-sequence
    observability the pit-diag stream never gave us (Block R; #397 guard).
    """
    if len(data) < 1:
        raise ValueError(f"0x4A4 needs >=1 byte, got {len(data)}")
    b = data[0]
    return {
        "air_negative": bool(b & 0x01),
        "air_positive": bool(b & 0x02),
        "precharge":    bool(b & 0x04),
        "ams_ok":       bool(b & 0x08),
    }


def decode_fdcan1_recovery(data: bytes) -> dict:
    """Decode 0x6C9 (#391/#392): FDCAN1 bus-off recovery diagnostics.
      bytes 0-3  busoff_recovery_count (LE u32) -- HAL_FDCAN_Stop/Start cycles
      bytes 4-7  acu_tx_fail_count     (LE u32) -- 0x12C ACU TX failures
    """
    if len(data) < 8:
        raise ValueError(f"0x6C9 needs 8 bytes, got {len(data)}")
    return {
        "busoff_recovery_count": int.from_bytes(data[0:4], "little"),
        "acu_tx_fail_count":     int.from_bytes(data[4:8], "little"),
    }


def decode_telem_temps(data: bytes) -> dict:
    """Decode 0x4A2 payload.
    Layout (post AMS PR #251 / #272):
      byte 0     min_tempC (int8)
      byte 1     max_tempC (int8)
      byte 2     avg_tempC (int8)
      bytes 3-4  dc_bus_V (little-endian uint16, volts)
      byte 5     cockpit byte (sentinel + mode_locked + TSMS + DASH_CHG bits)
      byte 6     reserved
      byte 7     heartbeat counter (0..255, wraps)

    The cockpit byte sub-fields per safety_task.cpp:
      bit 7   sentinel  — set whenever firmware is running (HIL_STUB-aware)
      bits 4..6 reserved (0)
      bits 2..3 mode_locked (0 Undecided / 1 Car / 2 Charger)
      bit 1   TSMS GPIO readback
      bit 0   DASH_CHG GPIO readback
    """
    if len(data) < 8:
        raise ValueError(f"0x4A2 needs 8 bytes, got {len(data)}")
    def i8(b): return b - 256 if b > 127 else b
    cb = data[5]
    return {
        "min_tempC": i8(data[0]),
        "max_tempC": i8(data[1]),
        "avg_tempC": i8(data[2]),
        "dc_bus_V":  data[3] | (data[4] << 8),
        "heartbeat": data[7],
        "cockpit": {
            "raw":   cb,
            "valid": bool(cb & 0x80),
            "mode":  (cb >> 2) & 0x03,
            "tsms":  bool(cb & 0x02),
            "dash":  bool(cb & 0x01),
        },
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
