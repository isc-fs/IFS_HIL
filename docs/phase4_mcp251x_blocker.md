# Phase 4 — mcp251x kernel-driver adoption on BACKPLANE_HIL

## Status

**Resolved.** All three MCP2515s on the PCB are bound to the kernel
`mcp251x` driver and appear as `can0`, `can1`, `can2` kernel netdevs
at boot. Loopback transmit/receive works on `can0` at 500 kbit/s.
The Rust `isc-fs/can-flasher` can attach via `-i socketcan -c canN`.

The path to the resolution is captured here because the diagnosis
took a significant detour and the fixes are non-obvious.

## Symptom

Stock `mcp251x` on Raspberry Pi OS kernel 6.12.47+rpt-rpi-v8, with a
custom dtoverlay mapping our three MCP2515s to `cs-gpios`
(GPIO27/17/18) and IRQs (GPIO4/5/6), failed probe with one of:

- `mcp251x spiN: MCP251x didn't enter in conf mode after reset` → `-110 ETIMEDOUT`
- `mcp251x spiN: Cannot initialize MCP2515. Wrong wiring?` → `-19 ENODEV`

Meanwhile, the existing register-level Python driver in
`tools/mcp2515.py` — via `/dev/spidev0.0` with `no_cs=True` and
manual GPIO CS — reads and writes the same chips cleanly and passes
the HIL test suite.

## What didn't cause it (ruled out)

Every time we iterated on one of these, the -110 timeout (and later
the -19 ENODEV) persisted identically:

- dtoverlay syntax (`-34 ERANGE` went away once `clocks = <&fixed-clock>;`
  was added; moot for later failures)
- `spidev0/1` collision (fixed via `status = "disabled"` fragments)
- `cs-gpios` polarity flag (ACTIVE_LOW vs ACTIVE_HIGH — kernel
  forcibly "enforces active low on GPIO handle" for the
  `microchip,mcp2515` binding; the DT flag is overridden)
- Pi 5VSBY under-dimensioned supply — swapped for a 3 A supply,
  `vcgencmd get_throttled = 0x0`, probe still failed identically
- PSU timing / crystal startup — delayed `modprobe` 76 s after boot,
  still failed
- Number of chips (single-chip vs three-chip overlay — same)
- SPI clock frequency (500 kHz through 10 MHz — same)
- SPI master (`spi-bcm2835` vs bitbanged `spi-gpio` — same `-110`
  on both; ruled out controller-specific quirks)
- IRQ type (`IRQ_TYPE_EDGE_FALLING` vs `IRQ_TYPE_LEVEL_LOW` — same)

## What caused it (three hardware-level quirks stacked)

Diagnosed from `ftrace` on `spi/*` tracepoints plus a Python
write-known-value / read-back experiment on `/dev/spidev0.0`:

### 1. Single multi-byte SPI reads corrupt the last byte

Through the PCB's SN74LVC125A MISO buffer, the BCM2835 SPI
controller's single `[cmd, addr, dummy]` 3-byte transfer loses or
corrupts the data byte. Writes of any length round-trip correctly —
only the last byte of a multi-byte transfer where the master samples
MISO is affected.

Splitting into `[cmd, addr]` + `[dummy]` with CS held low across
both (what our Python driver already does via two `xfer2` calls)
gives clean reads.

### 2. `RESET` instruction does not reliably enter CONFIG mode

The MCP2515 datasheet specifies that issuing the `0xC0` RESET SPI
instruction puts the chip in Configuration mode. On this board,
after RESET the chip stays in whatever prior mode it was in.
Explicitly writing `CANCTRL = 0x80` reliably transitions it to
CONFIG (CANSTAT reflects the mode change).

### 3. `CANCTRL` reads always return `0x00`

After any value is written to CANCTRL, reads of the register return
`0x00` instead of the written value. Writes still take effect — the
chip's operational mode matches what was written — but the stock
driver's power-up sanity check `(CANCTRL & 0x17) != 0x07` always
fails, producing `-ENODEV`. CANSTAT reads work correctly.

## The fix

Two artefacts on this branch:

- `infra/devicetree/mcp2515-triple.dts` — custom dtoverlay that
  declares all three MCP2515s on `cs-gpios = <27, 17, 18>` with
  level-low IRQs on GPIO4/5/6, `spi-cpol/spi-cpha` (mode 3), a
  shared 16 MHz `fixed-clock` node, and `pinctrl-0` on SPI0 that
  claims only MOSI/MISO/SCK so GPIO7/8 stay free for PSU_ON / PWR_OK.
- `infra/kernel-module/mcp251x-patched/` — out-of-tree module with
  four patches to `drivers/net/can/spi/mcp251x.c` (see that
  directory's README). The patch forces split reads, bootstraps
  CONFIG mode via explicit CANCTRL write, and skips the CANCTRL
  read-back sanity check.

### Required `/boot/firmware/config.txt` entries

```
dtoverlay=mcp2515-triple
gpio=7=op,dl        # PSU_ON# asserted at firmware stage
gpio=8=ip,pd        # PWR_OK as pulled-down input (not leftover SPI0_CE0)
```

### Install flow (on the Pi)

```
sudo cp infra/devicetree/mcp2515-triple.dtbo /boot/firmware/overlays/
# add the dtoverlay / gpio lines to /boot/firmware/config.txt
cd infra/kernel-module/mcp251x-patched && ./build.sh
sudo reboot
```

## Verification

After a clean boot:

```
$ sudo dmesg | grep mcp251x
mcp251x: loading out-of-tree module taints kernel.
mcp251x spi0.2 can0: MCP2515 successfully initialized.
mcp251x spi0.1 can1: MCP2515 successfully initialized.
mcp251x spi0.0 can2: MCP2515 successfully initialized.

$ sudo ip link set can0 up type can bitrate 500000 loopback on
$ cansend can0 123#DEADBEEFCAFEBABE ; candump -n 1 can0
 (0.000013)  can0  123   [8]  DE AD BE EF CA FE BA BE
```

## Follow-ups

- `can1` / `can2` intermittently show `NO-CARRIER` when all three are
  brought up simultaneously. `can0` is unaffected. Likely an
  IRQ-sharing or SPI bus arbitration quirk. Not a blocker for
  sequential flashing.
- Broker/dashboard/tests need their CAN backend migrated from
  register-level SPI to `python-can`-over-socketcan for the portion
  that touches the CAN chips. The other devices (DAC, ADC, INA226,
  TCA9555, nRF24) stay on register-level SPI.
- `systemd-networkd` units to bring `canN` up at 500 kbit/s on boot.
- `cargo install` `isc-fs/can-flasher` on the Pi, end-to-end flash
  test against an ECU.
