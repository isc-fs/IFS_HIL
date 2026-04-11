#!/usr/bin/env python3
"""
SPI debug tool — determines if MCP2515 RESET works and what CANSTAT reads.

Confirmed CS pins: GPIO17, GPIO18, GPIO27.
Tests:
  1. With GPIO16 HIGH (assumed GATE_OE): does RESET give CANSTAT=0x80?
  2. Raw CANCTRL read (0x0F) to see the full control register.
  3. Forced CANCTRL write to request config mode, then re-read CANSTAT.
  4. Inverse gate scan but reading raw resp bytes.

Run on RPi:
    python3 tools/scan_debug_spi.py
"""

import time
import spidev
import RPi.GPIO as GPIO

PSU_ON = 7
PWR_OK = 8
SPI_MISO, SPI_MOSI, SPI_SCLK = 9, 10, 11

CS_CAN1, CS_CAN2, CS_CAN3 = 17, 18, 27
CS_PINS = {CS_CAN1, CS_CAN2, CS_CAN3}

# All non-SPI, non-PSU, non-I2C GPIOs
SKIP = {SPI_MISO, SPI_MOSI, SPI_SCLK, 2, 3, PSU_ON, PWR_OK}
OTHER_GPIOS = [g for g in range(0, 28) if g not in SKIP and g not in CS_PINS]


def spi_xfer(spi, cs, data):
    GPIO.output(cs, GPIO.LOW)
    r = spi.xfer2(list(data))
    GPIO.output(cs, GPIO.HIGH)
    return r


def mcp_reset(spi, cs):
    spi_xfer(spi, cs, [0xC0])
    time.sleep(0.020)  # 20 ms — conservative


def mcp_read_reg(spi, cs, reg):
    """MCP2515 READ instruction: 0x03, reg, 0x00"""
    r = spi_xfer(spi, cs, [0x03, reg, 0x00])
    return r[2]


def mcp_write_reg(spi, cs, reg, value):
    """MCP2515 WRITE instruction: 0x02, reg, value"""
    spi_xfer(spi, cs, [0x02, reg, value])


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    GPIO.setup(PSU_ON, GPIO.OUT)
    GPIO.output(PSU_ON, GPIO.HIGH)
    GPIO.setup(PWR_OK, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    for cs in CS_PINS:
        GPIO.setup(cs, GPIO.OUT)
        GPIO.output(cs, GPIO.HIGH)

    for g in OTHER_GPIOS:
        GPIO.setup(g, GPIO.OUT)
        GPIO.output(g, GPIO.LOW)   # start LOW

    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = 500_000
    spi.mode = 0
    spi.no_cs = True
    spi.lsbfirst = False

    print("\n[PSU] Turning on...")
    GPIO.output(PSU_ON, GPIO.LOW)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if GPIO.input(PWR_OK):
            break
        time.sleep(0.05)
    print(f"[PSU] PWR_OK = {'HIGH' if GPIO.input(PWR_OK) else 'LOW'}")
    time.sleep(0.3)

    # ----------------------------------------------------------------
    # Test 1: Can we get CANSTAT=0x80 with GPIO16 as GATE_OE?
    # ----------------------------------------------------------------
    print("\n=== Test 1: GPIO16 HIGH as GATE_OE ===")
    GPIO.output(16, GPIO.HIGH)
    for cs, name in [(CS_CAN1, "CS_CAN1/GPIO17"),
                     (CS_CAN2, "CS_CAN2/GPIO18"),
                     (CS_CAN3, "CS_CAN3/GPIO27")]:
        mcp_reset(spi, cs)
        canstat = mcp_read_reg(spi, cs, 0x0E)
        canctrl = mcp_read_reg(spi, cs, 0x0F)
        print(f"  {name}: CANSTAT=0x{canstat:02X}  CANCTRL=0x{canctrl:02X}  "
              f"{'OK (config mode)' if canstat == 0x80 else '*** NOT config mode ***'}")
    GPIO.output(16, GPIO.LOW)

    # ----------------------------------------------------------------
    # Test 2: All candidates HIGH simultaneously
    # ----------------------------------------------------------------
    print("\n=== Test 2: ALL candidates HIGH simultaneously ===")
    for g in OTHER_GPIOS:
        GPIO.output(g, GPIO.HIGH)
    for cs, name in [(CS_CAN1, "CS_CAN1/GPIO17"),
                     (CS_CAN2, "CS_CAN2/GPIO18"),
                     (CS_CAN3, "CS_CAN3/GPIO27")]:
        mcp_reset(spi, cs)
        canstat = mcp_read_reg(spi, cs, 0x0E)
        canctrl = mcp_read_reg(spi, cs, 0x0F)
        print(f"  {name}: CANSTAT=0x{canstat:02X}  CANCTRL=0x{canctrl:02X}")
    # reset all back to LOW
    for g in OTHER_GPIOS:
        GPIO.output(g, GPIO.LOW)

    # ----------------------------------------------------------------
    # Test 3: Force config mode via CANCTRL write, check if it sticks
    # ----------------------------------------------------------------
    print("\n=== Test 3: Force config mode via CANCTRL write (GPIO16 HIGH) ===")
    GPIO.output(16, GPIO.HIGH)
    cs = CS_CAN1
    mcp_reset(spi, cs)
    canstat_before = mcp_read_reg(spi, cs, 0x0E)
    print(f"  After RESET: CANSTAT=0x{canstat_before:02X}")
    # write CANCTRL = 0x80 (REQOP=100 = config mode)
    mcp_write_reg(spi, cs, 0x0F, 0x80)
    time.sleep(0.005)
    canstat_after = mcp_read_reg(spi, cs, 0x0E)
    canctrl_after = mcp_read_reg(spi, cs, 0x0F)
    print(f"  After CANCTRL=0x80 write: CANSTAT=0x{canstat_after:02X}  CANCTRL=0x{canctrl_after:02X}")
    GPIO.output(16, GPIO.LOW)

    # ----------------------------------------------------------------
    # Test 4: Raw response bytes from READ CANSTAT (diagnose SPI noise)
    # ----------------------------------------------------------------
    print("\n=== Test 4: Raw SPI response bytes, GPIO16 HIGH ===")
    GPIO.output(16, GPIO.HIGH)
    for trial in range(5):
        GPIO.output(CS_CAN1, GPIO.LOW)
        r = spi.xfer2([0x03, 0x0E, 0x00])
        GPIO.output(CS_CAN1, GPIO.HIGH)
        print(f"  Trial {trial}: resp = {[hex(b) for b in r]}")
        time.sleep(0.002)
    GPIO.output(16, GPIO.LOW)

    # ----------------------------------------------------------------
    # Test 5: Negative gate scan — which pin, when de-asserted (LOW),
    # loses the 0x80 response (starts from all-HIGH state)
    # ----------------------------------------------------------------
    print("\n=== Test 5: Negative gate scan (find which pin LOW kills CANSTAT=0x80) ===")
    # Set all HIGH first
    for g in OTHER_GPIOS:
        GPIO.output(g, GPIO.HIGH)
    # Confirm baseline
    mcp_reset(spi, CS_CAN1)
    baseline = mcp_read_reg(spi, CS_CAN1, 0x0E)
    print(f"  Baseline (all HIGH): CANSTAT=0x{baseline:02X}")

    for g in OTHER_GPIOS:
        GPIO.output(g, GPIO.LOW)   # pull this one LOW
        mcp_reset(spi, CS_CAN1)
        canstat = mcp_read_reg(spi, CS_CAN1, 0x0E)
        label = "*** BUFFER LOST ***" if canstat != 0x80 else ""
        print(f"  GPIO{g:2d} LOW → CANSTAT=0x{canstat:02X}  {label}")
        GPIO.output(g, GPIO.HIGH)  # restore

    # ----------------------------------------------------------------
    print("\n[PSU] Off.")
    GPIO.output(PSU_ON, GPIO.HIGH)
    spi.close()
    GPIO.cleanup()


if __name__ == "__main__":
    main()
