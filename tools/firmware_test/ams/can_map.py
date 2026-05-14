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

# AMS state byte encoding (0x20 reply)
STATE_CPU_POWER         = 0x00
STATE_CPU_PRECHARGE     = 0x01
STATE_CPU_DISCONNECTED  = 0x02
STATE_CPU_ERROR         = 0x03
STATE_CPU_CHARGING      = 0x04

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
