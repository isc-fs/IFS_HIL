#!/usr/bin/env python3
"""
GPIO diagnostic — finds correct SPI_GATE_OE and PSU_ON pins.

Run on the RPi as root:
    sudo python3 tools/diagnose_gpio.py

What it does:
  1. Scans every free GPIO as SPI_GATE_OE candidate — the correct one
     makes the MCP2515 respond after reset (CANSTAT reads 0x80).
  2. Scans every free GPIO as PSU_ON candidate — driving it LOW should
     bring up the ATX PSU (PWRGOOD goes high, INA226 bus voltage > 1 V).
  3. Prints a full I2C bus scan using write_byte (more compatible).
"""

import time
import spidev
import smbus2
import RPi.GPIO as GPIO

# ---- SPI CS for MCP2515 #1 (confirmed from schematic) ----
CS_CAN1 = 12

# ---- All GPIOs that are not SPI bus or I2C bus ----
SPI_MISO, SPI_MOSI, SPI_SCLK = 9, 10, 11
I2C_SDA, I2C_SCL = 2, 3
SKIP = {SPI_MISO, SPI_MOSI, SPI_SCLK, I2C_SDA, I2C_SCL, CS_CAN1}

CANDIDATES = [g for g in range(0, 28) if g not in SKIP]

# Known CS pins — keep them HIGH (deselected) during scan
ALL_CS = [12, 21, 20, 19, 18, 17, 25, 15, 14, 26, 8, 13]

# ---- MCP2515 helpers ----
def mcp_reset(spi, cs):
    GPIO.output(cs, GPIO.LOW)
    spi.xfer2([0xC0])          # RESET instruction
    GPIO.output(cs, GPIO.HIGH)
    time.sleep(0.01)

def mcp_read_canstat(spi, cs):
    GPIO.output(cs, GPIO.LOW)
    resp = spi.xfer2([0x03, 0x0E, 0x00])   # READ CANSTAT
    GPIO.output(cs, GPIO.HIGH)
    return resp[2]

# ---- INA226 bus voltage helper ----
def ina_bus_voltage(bus, addr):
    try:
        raw = bus.read_i2c_block_data(addr, 0x02, 2)
        val = (raw[0] << 8) | raw[1]
        return val * 1.25e-3
    except Exception:
        return -1.0

# ---- I2C full scan ----
def i2c_scan(bus):
    found = []
    for addr in range(0x08, 0x78):
        try:
            bus.write_byte(addr, 0)
            found.append(addr)
        except OSError:
            pass
    return found


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    # Set all CS pins high
    for cs in ALL_CS:
        GPIO.setup(cs, GPIO.OUT)
        GPIO.output(cs, GPIO.HIGH)

    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = 500_000
    spi.mode = 0
    spi.no_cs = True

    i2c = smbus2.SMBus(1)

    # ------------------------------------------------------------------
    print("\n=== I2C bus scan (write_byte probe) ===")
    found = i2c_scan(i2c)
    print(f"  Found {len(found)} device(s): {[hex(a) for a in found]}")

    # ------------------------------------------------------------------
    print("\n=== Scanning for SPI_GATE_OE ===")
    print("  (correct GPIO makes MCP2515 CANSTAT read 0x80 after reset)\n")

    gate_found = None
    for gpio in CANDIDATES:
        GPIO.setup(gpio, GPIO.OUT)
        GPIO.output(gpio, GPIO.HIGH)   # assert OE

        mcp_reset(spi, CS_CAN1)
        canstat = mcp_read_canstat(spi, CS_CAN1)

        GPIO.output(gpio, GPIO.LOW)    # de-assert
        status = "✓ MATCH" if canstat == 0x80 else f"  0x{canstat:02X}"
        print(f"  GPIO{gpio:2d}  CANSTAT={hex(canstat)}  {status}")

        if canstat == 0x80 and gate_found is None:
            gate_found = gpio

        GPIO.setup(gpio, GPIO.IN)

    print(f"\n  >>> SPI_GATE_OE = GPIO{gate_found}" if gate_found else
          "\n  >>> SPI_GATE_OE not found — check schematic")

    # ------------------------------------------------------------------
    print("\n=== Scanning for PSU_ON ===")
    print("  (correct GPIO driven LOW turns on ATX PSU — INA226 0x40 Vbus > 1 V)\n")

    psu_found = None
    ina_addr  = 0x40   # INA226 on 12 V rail

    for gpio in CANDIDATES:
        GPIO.setup(gpio, GPIO.OUT)
        GPIO.output(gpio, GPIO.LOW)    # drive LOW → PS_ON# asserted
        time.sleep(0.1)

        v = ina_bus_voltage(i2c, ina_addr)
        GPIO.output(gpio, GPIO.HIGH)   # release
        time.sleep(0.05)

        status = "✓ MATCH" if v > 1.0 else f"  {v:.2f} V"
        print(f"  GPIO{gpio:2d}  INA226_12V Vbus={v:.3f} V  {status}")

        if v > 1.0 and psu_found is None:
            psu_found = gpio

        GPIO.setup(gpio, GPIO.IN)

    print(f"\n  >>> PSU_ON = GPIO{psu_found}" if psu_found else
          "\n  >>> PSU_ON not found — check if ATX PSU is connected")

    # ------------------------------------------------------------------
    print("\n=== Summary ===")
    print(f"  SPI_GATE_OE = GPIO{gate_found}")
    print(f"  PSU_ON      = GPIO{psu_found}")
    print(f"  I2C devices = {[hex(a) for a in found]}")
    print()

    spi.close()
    i2c.close()
    GPIO.cleanup()


if __name__ == "__main__":
    main()
