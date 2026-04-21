# `mcp251x-patched` — kernel module for BACKPLANE_HIL

Out-of-tree build of the Linux `mcp251x` driver with five surgical
patches required to make the three MCP2515 CAN controllers on the
BACKPLANE_HIL PCB probe and operate under `SocketCAN`.

## Why

Stock `mcp251x` on Raspberry Pi OS (kernel 6.12.47+rpt-rpi-v8) probes
fail with either `-ETIMEDOUT` (didn't enter config after reset) or
`-ENODEV` (wrong wiring) on this PCB. Root causes, confirmed via
ftrace and Python SPI experiments:

1. The Pi's BCM2835 SPI controller, driving the three MCP2515s through
   the board's SN74LVC125A MISO buffer, **corrupts the last byte of a
   single multi-byte SPI transfer**. Writes of any length work; single
   `[cmd, addr, dummy]` 3-byte reads lose or garble the data byte.
   Our register-level Python driver avoids this by splitting every
   read into `[cmd, addr]` then `[dummy]`, keeping CS asserted across
   both. The kernel driver doesn't — it uses a single `spi_sync`.
2. The MCP2515 `RESET` SPI instruction (`0xC0`) does not reliably put
   the chip into Configuration mode on this board. Explicitly writing
   `CANCTRL = 0x80` does.
3. Reads of `CANCTRL` register always return `0x00` on this board
   regardless of the value actually written (the chip honours the
   write — `CANSTAT` reflects the requested mode — but read-back of
   `CANCTRL` is stuck). The stock driver uses `CANCTRL` read as a
   power-up sanity check, which always fails here.
4. `ip link set canN up` after a previous `down` calls `mcp251x_hw_wake`,
   which tries to wake the chip from SLEEP via a `CANINTE`/`CANINTF`
   WAKIE-trigger write followed by `CANCTRL = 0x80`. On this hardware
   the chip does not reliably wake via that mechanism — the oscillator
   stays stopped and the subsequent CANCTRL write times out. The same
   symptom was already known for the register-level Python driver,
   where `reset()` is issued on any SLEEP → target-mode transition.

## What the patch does

See `0001-backplane-hil-spi-quirks.patch`:

1. `mcp251x_read_reg` — always use `spi_write_then_read` (split),
   bypassing the `SPI_CONTROLLER_HALF_DUPLEX` gate.
2. `mcp251x_read_2regs` — same split treatment.
3. `mcp251x_hw_reset` — after the RESET instruction and OST delay,
   explicitly write `CANCTRL = CANCTRL_REQOP_CONF | 0x07` (which is
   `0x87`: REQOP_CONF + CLKEN + CLKPRE_8, matching the chip's
   power-up default). The subsequent `CANSTAT` poll then sees
   `CANCTRL_REQOP_CONF` reliably.
4. `mcp251x_hw_probe` — skip the `(CANCTRL & 0x17) != 0x07` sanity
   check. The CANSTAT-based CONFIG-mode verification already proves
   the chip is alive.
5. `mcp251x_hw_wake` — replace the WAKIE/WAKIF + `CANCTRL` write with
   a full `mcp251x_hw_reset()` call. The hardware reset restarts the
   oscillator cleanly (unlike the WAKIE trick on this board) and the
   patched reset path already handles the CONFIG-mode transition.
   A forward declaration of `mcp251x_hw_reset` is added above
   `mcp251x_hw_wake` to satisfy C ordering.

Every change is annotated with a `/* Patched: … */` comment in the
source that references this README.

## Build & install (on the Pi)

```sh
sudo apt-get install -y linux-headers-$(uname -r) curl xz-utils make gcc
cd infra/kernel-module/mcp251x-patched
./build.sh
sudo modprobe -r mcp251x && sudo modprobe mcp251x
```

`build.sh` will keep a backup of the stock module at
`/lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz.orig`
the first time it runs.

## Verifying after install

With the `mcp2515-triple` dtoverlay loaded (see
`infra/devicetree/mcp2515-triple.dts`):

```sh
dmesg | grep mcp251x
# Expect: "MCP2515 successfully initialized" for spi0.0, spi0.1, spi0.2
ip -br link | grep can
# Expect: can0, can1, can2 present (DOWN until brought up)
sudo ip link set can0 up type can bitrate 500000 loopback on
cansend can0 123#DEADBEEF &
candump -n 1 can0
```

## Kernel updates

The patch targets `rpi-6.12.y`. When the running kernel changes,
re-run `build.sh`. If `patch` fails to apply because upstream has
changed the surrounding lines, rebase the hunks manually — the four
patched regions are small and comments in the source make them easy
to locate.

Also regenerate the patch after any local edits:

```sh
diff -u _build/.mcp251x.c.orig _build/mcp251x.c > 0001-backplane-hil-spi-quirks.patch
```
