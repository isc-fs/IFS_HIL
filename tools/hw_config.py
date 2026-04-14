"""
Hardware pin and address configuration for the BACKPLANE_HIL PCB.

All assignments derived from BACKPLANE_HIL.kicad_pcb pad-to-net mapping (J2)
and confirmed by hardware SPI scan. PCB is the authoritative source.

RPi GPIO numbering is BCM throughout.

SPI bus: hardware SPI0 (MISO=GPIO9, MOSI=GPIO10, SCLK=GPIO11).
All chip-selects are software GPIO (active-low).

I2C bus: hardware I2C1 (/dev/i2c-1, SDA=GPIO2, SCL=GPIO3).

MISO BUFFER (IC1, SN74LVC125A):
  The buffer ~OE is driven by the ATX PWR_OK signal through Q5 (NMOS).
  When PSU is on (PWR_OK HIGH), Q5 pulls all four ~OE pins LOW → buffer enabled.
  This is HARDWARE-CONTROLLED; no GPIO management is required in software.
  Simply power on the PSU and the MISO buffer is automatically active.
"""

# ---------------------------------------------------------------------------
# SPI bus
# ---------------------------------------------------------------------------
SPI_BUS = 0          # /dev/spidev0.x
SPI_DEVICE = 0       # opened as spidev0.0; CS managed manually
SPI_MAX_HZ = 1_000_000  # conservative 1 MHz for all peripherals

# ---------------------------------------------------------------------------
# SPI chip-selects  (active LOW — drive low to select, high to deselect)
# ---------------------------------------------------------------------------

# MCP2515 CAN controllers (U17=CAN1, U19=CAN2, U21=CAN3)
# Source: BACKPLANE_HIL.kicad_pcb — J2 pad 13 (/CS_CAN1=GPIO27),
#         pad 11 (/CS_CAN2=GPIO17), pad 12 (/CS_CAN3=GPIO18).
# Hardware SPI scan confirmed all three have MCP2515 responding CANSTAT=0x80.
CS_CAN1 = 27    # GPIO27 — PCB confirmed (J2 pad 13)
CS_CAN2 = 17    # GPIO17 — PCB confirmed (J2 pad 11)
CS_CAN3 = 18    # GPIO18 — PCB confirmed (J2 pad 12)

# MCP3208 ADCs (U9=ADC1, U10=ADC2, U11=ADC3)
# Source: BACKPLANE_HIL.kicad_pcb — J2 pad 35 (/CS_ADC1=GPIO19),
#         pad 38 (/CS_ADC2=GPIO20), pad 40 (/CS_ADC3=GPIO21).
CS_ADC1 = 19    # GPIO19 — PCB confirmed (J2 pad 35)
CS_ADC2 = 20    # GPIO20 — PCB confirmed (J2 pad 38)
CS_ADC3 = 21    # GPIO21 — PCB confirmed (J2 pad 40)

# DAC80504 DACs (U12=DAC1, U13=DAC2, U14=DAC3, U15=DAC4)
# Source: BACKPLANE_HIL.kicad_pcb — J2 pad 15 (/CS_DAC1=GPIO22),
#         pad 16 (/CS_DAC2=GPIO23), pad 18 (/CS_DAC3=GPIO24),
#         pad 22 (/CS_DAC4=GPIO25).
# NOTE: GPIO22-25 driving the MISO bus during SPI CAN scans explained the
# "BUFFER LOST" anomaly — they share the SPI bus and asserting them
# simultaneously with a CAN CS causes bus contention.
CS_DAC1 = 22    # GPIO22 — PCB confirmed (J2 pad 15)
CS_DAC2 = 23    # GPIO23 — PCB confirmed (J2 pad 16)
CS_DAC3 = 24    # GPIO24 — PCB confirmed (J2 pad 18)
CS_DAC4 = 25    # GPIO25 — PCB confirmed (J2 pad 22)

# nRF24L01 breakout (U23)
# Source: BACKPLANE_HIL.kicad_pcb — J2 pad 37 (NRF24_RX_CS=GPIO26),
#         pad 33 (NRF24_RX_CE=GPIO13), pad 32 (NRF24_RX_IRQ=GPIO12).
NRF24_CS  = 26  # GPIO26 — PCB confirmed (J2 pad 37)
NRF24_CE  = 13  # GPIO13 — PCB confirmed (J2 pad 33)
NRF24_IRQ = 12  # GPIO12 — PCB confirmed (J2 pad 32, input)

# ---------------------------------------------------------------------------
# MCP2515 interrupt inputs  (active LOW from CAN controller INT pin)
# ---------------------------------------------------------------------------
# Source: BACKPLANE_HIL.kicad_pcb — J2 pad 7 (/INT_CAN1=GPIO4),
#         pad 29 (/INT_CAN2=GPIO5), pad 31 (/INT_CAN3=GPIO6).
INT_CAN1 = 4    # GPIO4  — PCB confirmed (J2 pad 7)
INT_CAN2 = 5    # GPIO5  — PCB confirmed (J2 pad 29)
INT_CAN3 = 6    # GPIO6  — PCB confirmed (J2 pad 31)

# ---------------------------------------------------------------------------
# Power control
# ---------------------------------------------------------------------------
# Source: BACKPLANE_HIL.kicad_pcb — J2 pad 26 (/PSU_ON=GPIO7, SPI CE1),
#         pad 24 (/PWR_OK=GPIO8, SPI CE0). Confirmed by user.
PSU_ON = 7      # GPIO7  — drives ATX PS_ON# low to turn on PSU (active LOW to PSU)
PWR_OK = 8      # GPIO8  — ATX PWRGOOD signal read back (HIGH = PSU rails stable)

# ---------------------------------------------------------------------------
# I2C bus  (/dev/i2c-1, GPIO2=SDA, GPIO3=SCL)
# ---------------------------------------------------------------------------
I2C_BUS = 1

# INA226 power monitors — one per MLC carrier slot (STM32 boards).
# Each INA226 measures current/power consumed by the carrier, not the PSU rail.
# Wired for low-side current sensing; bus_voltage() reads ~0 V by design.
# Address = 0x40 | (A1<<1) | A0, confirmed by I2C scan.
INA226_ADDR_MLC1 = 0x40   # MLC1 carrier — A1=GND, A0=GND
INA226_ADDR_MLC2 = 0x41   # MLC2 carrier — A1=GND, A0=VS
INA226_ADDR_MLC3 = 0x44   # MLC3 carrier — A1=VS,  A0=GND
INA226_ADDR_MLC4 = 0x45   # MLC4 carrier — A1=VS,  A0=VS

# INA226 shunt resistor value (Ohms) — VERIFY against BOM
INA226_SHUNT_OHM = 0.01   # 10 mΩ  (adjust per actual fitted value)

# TCA9555 I/O expanders (U3, U6, U8) — address = 0x20 | (A2<<2) | (A1<<1) | A0
TCA9555_ADDR_0 = 0x20   # U3: A2=A1=A0=GND — confirmed by I2C scan
TCA9555_ADDR_1 = 0x21   # U6: A2=A1=GND, A0=VS — confirmed by I2C scan
TCA9555_ADDR_2 = 0x22   # U8: A2=GND, A1=VS, A0=GND — confirmed by I2C scan

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
# MLC carrier current limits (A) — flag unexpected consumption
# ---------------------------------------------------------------------------
# Adjust per-carrier once nominal draw is characterised.
MLC_CURRENT_MAX_A = 3.0   # trip threshold for overcurrent warning
