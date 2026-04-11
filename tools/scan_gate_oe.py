#!/usr/bin/env python3
"""
SPI_GATE_OE finder — inverse scan.

We already know CS_CAN1/2/3 = GPIO17/18/27 from the 2D sweep.
This script finds SPI_GATE_OE by:
  1. Holding ALL non-SPI, non-CS GPIOs LOW (buffer disabled by default).
  2. Keeping CS pins HIGH (deselected).
  3. Raising each candidate GPIO one at a time.
  4. The GPIO that makes MCP2515 CANSTAT read 0x80 is SPI_GATE_OE.

Run on RPi:
    python3 tools/scan_gate_oe.py
"""

import time
import spidev
import RPi.GPIO as GPIO

PSU_ON = 7     # active LOW — drive LOW to turn on ATX PSU
PWR_OK = 8     # active HIGH — HIGH means rails are stable

SPI_MISO, SPI_MOSI, SPI_SCLK = 9, 10, 11

# Confirmed CS pins from previous scan — keep HIGH (deselected)
CS_CAN1, CS_CAN2, CS_CAN3 = 17, 18, 27
CS_PINS = {CS_CAN1, CS_CAN2, CS_CAN3}

# I2C pins — leave alone
I2C_SDA, I2C_SCL = 2, 3

# Pins to skip entirely
SKIP = {SPI_MISO, SPI_MOSI, SPI_SCLK, I2C_SDA, I2C_SCL, PSU_ON, PWR_OK}

# These will be tested as GATE_OE candidates (driven LOW by default)
CANDIDATES = [g for g in range(0, 28) if g not in SKIP and g not in CS_PINS]


def mcp_reset_and_read(spi, cs_pin):
    GPIO.output(cs_pin, GPIO.LOW)
    spi.xfer2([0xC0])                    # RESET
    GPIO.output(cs_pin, GPIO.HIGH)
    time.sleep(0.015)
    GPIO.output(cs_pin, GPIO.LOW)
    resp = spi.xfer2([0x03, 0x0E, 0x00]) # READ CANSTAT
    GPIO.output(cs_pin, GPIO.HIGH)
    return resp[2]


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    # PSU control
    GPIO.setup(PSU_ON, GPIO.OUT)
    GPIO.output(PSU_ON, GPIO.HIGH)       # keep OFF initially
    GPIO.setup(PWR_OK, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    # CS pins — HIGH (deselected)
    for cs in CS_PINS:
        GPIO.setup(cs, GPIO.OUT)
        GPIO.output(cs, GPIO.HIGH)

    # All GATE_OE candidates — LOW (buffer disabled by default)
    for g in CANDIDATES:
        GPIO.setup(g, GPIO.OUT)
        GPIO.output(g, GPIO.LOW)

    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = 500_000
    spi.mode = 0
    spi.no_cs = True
    spi.lsbfirst = False

    # Power on PSU
    print("\n[PSU] Asserting PSU_ON (GPIO7 LOW)...")
    GPIO.output(PSU_ON, GPIO.LOW)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if GPIO.input(PWR_OK) == GPIO.HIGH:
            break
        time.sleep(0.05)
    pwr_ok = GPIO.input(PWR_OK) == GPIO.HIGH
    print(f"[PSU] PWR_OK = {'HIGH — rails stable' if pwr_ok else 'LOW — check PSU'}")
    if not pwr_ok:
        print("[PSU] WARNING: Continuing anyway.")
    time.sleep(0.2)

    print("\n=== Inverse GATE_OE scan ===")
    print("All candidates LOW (buffer off). Raising one at a time.")
    print("GPIO that enables CANSTAT=0x80 is SPI_GATE_OE.\n")

    gate_found = None

    for g in CANDIDATES:
        GPIO.output(g, GPIO.HIGH)        # tentatively enable buffer via this pin

        canstat = mcp_reset_and_read(spi, CS_CAN1)
        match = canstat == 0x80
        label = "*** GATE_OE FOUND ***" if match else ""
        print(f"  GPIO{g:2d}  CANSTAT=0x{canstat:02X}  {'MATCH' if match else '.'} {label}")

        if match and gate_found is None:
            gate_found = g

        GPIO.output(g, GPIO.LOW)         # restore LOW

    print()
    if gate_found is not None:
        print(f"  >>> SPI_GATE_OE = GPIO{gate_found}")
        # Confirm with all three CS pins
        GPIO.output(gate_found, GPIO.HIGH)
        for cs, name in [(CS_CAN1, "CS_CAN1"), (CS_CAN2, "CS_CAN2"), (CS_CAN3, "CS_CAN3")]:
            v = mcp_reset_and_read(spi, cs)
            print(f"  {name} (GPIO{cs})  CANSTAT=0x{v:02X}  {'OK' if v == 0x80 else 'FAIL'}")
        GPIO.output(gate_found, GPIO.LOW)
    else:
        print("  >>> SPI_GATE_OE not found.")
        print("  Possible causes:")
        print("  - Buffer ~OE is permanently tied LOW (always enabled)")
        print("  - GATE_OE is one of the skipped pins")
        print("  - Q5 NMOS not fitted or open-circuit")

    print("\n[PSU] Turning off PSU.")
    GPIO.output(PSU_ON, GPIO.HIGH)

    spi.close()
    GPIO.cleanup()


if __name__ == "__main__":
    main()
