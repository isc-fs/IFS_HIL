"""
Hardware pin and address configuration for the BACKPLANE_HIL PCB.

Derived from schematic coordinate analysis of rpi.kicad_sch.
Lines marked VERIFY should be confirmed against the physical PCB before running tests.

RPi GPIO numbering is BCM throughout.

SPI bus: hardware SPI0 (MISO=GPIO9, MOSI=GPIO10, SCLK=GPIO11).
All chip-selects are software GPIO (active-low).

I2C bus: hardware I2C1 (/dev/i2c-1, SDA=GPIO2, SCL=GPIO3).
"""

# ---------------------------------------------------------------------------
# SPI bus
# ---------------------------------------------------------------------------
SPI_BUS = 0          # /dev/spidev0.x
SPI_DEVICE = 0       # opened as spidev0.0; CS managed manually
SPI_MAX_HZ = 1_000_000  # conservative 1 MHz for all peripherals

# MISO is gated by SN74LVC125A (IC1), enabled when SPI_GATE_OE is HIGH.
# Q5 (NMOS) pulls IC1 ~OE low when SPI_GATE_OE is asserted.
SPI_GATE_OE = 16     # GPIO16 — VERIFY against schematic

# ---------------------------------------------------------------------------
# SPI chip-selects  (active LOW — drive low to select, high to deselect)
# ---------------------------------------------------------------------------

# MCP2515 CAN controllers (U17, U19, U21)
CS_CAN1 = 12    # GPIO12 — confirmed from schematic y=81.28
CS_CAN2 = 21    # GPIO21 — confirmed from schematic y=53.34
CS_CAN3 = 20    # GPIO20 — confirmed from schematic y=55.88

# MCP3208 ADCs (U9, U10, U11)
CS_ADC1 = 19    # GPIO19 — confirmed from schematic y=58.42
CS_ADC2 = 18    # GPIO18 — confirmed from schematic y=60.96
CS_ADC3 = 17    # GPIO17 — confirmed from schematic y=63.5

# DAC80504 DACs (U12, U13, U14, U15)
CS_DAC1 = 25    # GPIO25 — VERIFY (schematic y=68.58 has no direct GPIO pin)
CS_DAC2 = 15    # GPIO15 — confirmed from schematic y=71.12
CS_DAC3 = 14    # GPIO14 — confirmed from schematic y=73.66
CS_DAC4 = 26    # GPIO26 — VERIFY (schematic y=76.2 has no direct GPIO pin)

# nRF24L01 breakout (U23)
NRF24_CS = 8    # GPIO8 (CE0_SPI0) — VERIFY
NRF24_CE = 13   # GPIO13 — confirmed from schematic y=78.74

# ---------------------------------------------------------------------------
# MCP2515 interrupt inputs  (active LOW from CAN controller INT pin)
# ---------------------------------------------------------------------------
INT_CAN1 = 4    # GPIO4  — confirmed from schematic y=58.42
INT_CAN2 = 22   # GPIO22 — VERIFY
INT_CAN3 = 27   # GPIO27 — VERIFY

# ---------------------------------------------------------------------------
# Power control
# ---------------------------------------------------------------------------
# PSU_ON: drives ATX PS_ON# low to turn on the ATX PSU (active LOW to PSU)
PSU_ON  = 5     # GPIO5 — VERIFY
# PWR_OK: ATX PWRGOOD signal read back (HIGH = PSU rails stable)
PWR_OK  = 6     # GPIO6 — VERIFY

# ---------------------------------------------------------------------------
# I2C bus  (/dev/i2c-1, GPIO2=SDA, GPIO3=SCL)
# ---------------------------------------------------------------------------
I2C_BUS = 1

# INA226 power monitors (U1, U2, U4) — address = 0x40 | (A1<<1) | A0
# Addresses determined by A0/A1 pin strapping to GND(0) or VS(1):
INA226_ADDR_12V  = 0x40   # U4: A1=GND, A0=GND — VERIFY
INA226_ADDR_5V   = 0x41   # U2: A1=GND, A0=VS  — VERIFY
INA226_ADDR_3V3  = 0x44   # U1: A1=VS,  A0=GND — VERIFY

# INA226 shunt resistor values (Ohms) — VERIFY against BOM
INA226_SHUNT_OHM = 0.01   # 10 mΩ  (adjust per actual fitted value)

# TCA9555 I/O expanders (U3, U6, U8) — address = 0x20 | (A2<<2) | (A1<<1) | A0
TCA9555_ADDR_0 = 0x20   # U3: A2=A1=A0=GND — VERIFY
TCA9555_ADDR_1 = 0x21   # U6: A2=A1=GND, A0=VS — VERIFY
TCA9555_ADDR_2 = 0x22   # U8: A2=GND, A1=VS, A0=GND — VERIFY

# ---------------------------------------------------------------------------
# Relay control
# Relays K1-K4 are driven by Q1-Q4 NMOSFETs whose gates come from TCA9555
# outputs.  Adjust port/pin mapping to match actual TCA9555 wiring.
# TCA9555 port 0 = pins IO0_0..IO0_7, port 1 = IO1_0..IO1_7
# ---------------------------------------------------------------------------
# (expander addr, port, bit) for each relay coil driver
RELAY_PINS = {
    "K1": (TCA9555_ADDR_0, 0, 0),   # VERIFY
    "K2": (TCA9555_ADDR_0, 0, 1),   # VERIFY
    "K3": (TCA9555_ADDR_0, 0, 2),   # VERIFY
    "K4": (TCA9555_ADDR_0, 0, 3),   # VERIFY
}

# ---------------------------------------------------------------------------
# MCP2515 oscillator frequency (Hz) — matches Y1/Y2/Y3 crystals on PCB
# ---------------------------------------------------------------------------
MCP2515_OSC_HZ = 16_000_000

# CAN bus bitrate for loopback test
CAN_BITRATE = 500_000

# ---------------------------------------------------------------------------
# Power rail expected voltages (V) — used for PASS/FAIL in rail tests
# ---------------------------------------------------------------------------
RAIL_LIMITS = {
    "12V":  (11.4, 12.6),
    "5V":   (4.75, 5.25),
    "3V3":  (3.135, 3.465),
}
