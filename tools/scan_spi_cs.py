#!/usr/bin/env python3
"""
SPI chip-select + GATE_OE scanner.

Powers on the ATX PSU via PSU_ON=GPIO7, waits for PWRGOOD=GPIO8,
then does a 2-D sweep over all GPIO pairs to find:
  - SPI_GATE_OE  (enables IC1 SN74LVC125A MISO buffer)
  - CS_CAN1/2/3  (MCP2515 chip-selects — CANSTAT reads 0x80 after reset)

Run on RPi:
    python3 tools/scan_spi_cs.py
"""

import time
import spidev
import RPi.GPIO as GPIO

PSU_ON = 7    # active LOW — drive LOW to turn on ATX PSU
PWR_OK = 8    # active HIGH — HIGH means rails are stable

SPI_MISO, SPI_MOSI, SPI_SCLK = 9, 10, 11
SKIP = {SPI_MISO, SPI_MOSI, SPI_SCLK, PSU_ON, PWR_OK}
ALL_GPIO = [g for g in range(0, 28) if g not in SKIP]


def mcp_reset_and_read(spi, cs_pin):
    GPIO.output(cs_pin, GPIO.LOW)
    spi.xfer2([0xC0])                          # RESET
    GPIO.output(cs_pin, GPIO.HIGH)
    time.sleep(0.015)
    GPIO.output(cs_pin, GPIO.LOW)
    resp = spi.xfer2([0x03, 0x0E, 0x00])       # READ CANSTAT
    GPIO.output(cs_pin, GPIO.HIGH)
    return resp[2]


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    # PSU control
    GPIO.setup(PSU_ON, GPIO.OUT)
    GPIO.output(PSU_ON, GPIO.HIGH)             # keep OFF initially
    GPIO.setup(PWR_OK, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    # All other GPIOs as outputs HIGH (deselected)
    for g in ALL_GPIO:
        GPIO.setup(g, GPIO.OUT)
        GPIO.output(g, GPIO.HIGH)

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
    print(f"[PSU] PWR_OK = {'HIGH — rails stable' if pwr_ok else 'LOW — check PSU connection'}")
    if not pwr_ok:
        print("[PSU] WARNING: Continuing anyway — PWR_OK may not be wired or PSU takes longer.")
    time.sleep(0.2)   # let rails settle

    # 2-D scan: gate_gpio × cs_gpio
    print("\n=== 2-D scan: SPI_GATE_OE × CS_CAN ===")
    print("Looking for CANSTAT=0x80 (MCP2515 config mode after reset)\n")

    results = []   # (gate, cs, canstat)

    for gate in ALL_GPIO:
        GPIO.output(gate, GPIO.HIGH)           # assert gate OE
        hit_this_gate = False
        for cs in ALL_GPIO:
            if cs == gate:
                continue
            canstat = mcp_reset_and_read(spi, cs)
            if canstat == 0x80:
                print(f"  MATCH  GATE_OE=GPIO{gate:2d}  CS=GPIO{cs:2d}  CANSTAT=0x{canstat:02X}")
                results.append((gate, cs))
                hit_this_gate = True
        if not hit_this_gate:
            pass   # no match for this gate — skip printing to keep output clean
        GPIO.output(gate, GPIO.LOW)            # de-assert gate

    print(f"\n=== Results: {len(results)} match(es) ===")
    for gate, cs in results:
        print(f"  GATE_OE=GPIO{gate}  CS=GPIO{cs}")

    if len(results) >= 3:
        gates = list(dict.fromkeys(g for g, _ in results))
        css   = [cs for _, cs in results]
        print(f"\n  SPI_GATE_OE = GPIO{gates[0]}")
        for i, cs in enumerate(css[:3]):
            print(f"  CS_CAN{i+1}     = GPIO{cs}")
    elif not results:
        print("\n  No MCP2515 found with PSU on.")
        print("  Possible causes:")
        print("  - IC1 (SN74LVC125A) MISO buffer always disabled")
        print("  - MCP2515 crystals not oscillating")
        print("  - SPI wiring issue")

    # Power off PSU safely
    print("\n[PSU] De-asserting PSU_ON (GPIO7 HIGH) — PSU off.")
    GPIO.output(PSU_ON, GPIO.HIGH)

    spi.close()
    GPIO.cleanup()


if __name__ == "__main__":
    main()
