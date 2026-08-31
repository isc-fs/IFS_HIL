# The `mcp251x` kernel-driver patches

The Linux `mcp251x` driver on Raspberry Pi OS cannot drive the three
MCP2515s on the BACKPLANE_HIL PCB unaltered. Five targeted patches,
shipped as an out-of-tree module at
[`infra/kernel-module/mcp251x-patched/`](../../infra/kernel-module/mcp251x-patched/),
work around three hardware-level quirks specific to this board.

This document explains each quirk, each patch, and how to maintain
the module across kernel updates. Operators who just want to
"install it and move on" should follow the install recipe in
[`getting-started.md`](../getting-started.md) — the README in the
module directory is the minimal pointer; the rest of this document
is the "why" behind those instructions.

---

## The three hardware quirks

### 1. Single multi-byte SPI reads drop the last byte

Through the PCB's `SN74LVC125A` MISO buffer, the Raspberry Pi's
`spi-bcm2835` controller corrupts the final byte of a single
multi-byte SPI transaction. Symptoms:

- **Writes of any length** round-trip correctly (the master doesn't
  sample MISO, so nothing reads back). Verified with a
  write-known-value / split-read-back experiment on CANINTE.
- **Single `[cmd, addr, dummy]` 3-byte reads** return garbage for
  the data byte. In SPI mode 0, the MSB is dropped; in mode 3, an
  extra bit sometimes appears in the MSB.
- **Two-transfer reads** — `spi_write_then_read(spi, [cmd, addr],
  &val, 1)` — round-trip correctly in mode 3. This is the pattern
  the register-level Python driver already used via two separate
  `xfer2` calls with CS held low across both.

Root cause is at the bus-electrical level (buffer propagation
delay + CS/SCK setup timing combining with the Pi's transfer
chunking). Verified with `ftrace` on the SPI tracepoints during a
failing probe — confirmed the kernel sends the right bytes and
the chip returns the wrong ones on the read half.

### 2. The MCP2515 `RESET` instruction is unreliable here

Per the datasheet, sending instruction byte `0xC0` over SPI should
put the chip into CONFIG mode immediately. On this board the chip
does not always transition — sometimes it stays in whatever mode
it was in previously.

Writing `CANCTRL = 0x80` (REQOP = CONFIG) directly does transition
the chip reliably. `CANSTAT` reflects the new mode immediately
after the write.

The effect combined with quirk #1: the stock driver issues RESET,
then reads CANSTAT to confirm CONFIG mode. Both ops fail — the
chip didn't reset, and even if it had the readback would be
garbled.

### 3. `CANCTRL` reads always return `0x00` on this board

After any value is written to `CANCTRL`, subsequent reads of
`CANCTRL` return `0x00` regardless of what was written. Writes
still take effect — the chip's operational mode matches the
written value, and `CANSTAT` reads correctly. Only the `CANCTRL`
register itself reads as stuck.

The stock driver's `mcp251x_hw_probe` reads `CANCTRL` expecting
the power-up default pattern and returns `-ENODEV` if it doesn't
match. On this board that check always fails.

---

## The five patches

See
[`infra/kernel-module/mcp251x-patched/0001-backplane-hil-spi-quirks.patch`](../../infra/kernel-module/mcp251x-patched/0001-backplane-hil-spi-quirks.patch)
for the literal diff against the rpi-6.12.y upstream. Every hunk
carries a `/* Patched: ... */` comment pointing at this
document.

### Patch 1 — `mcp251x_read_reg`: always split

Rewrites register reads to always use `spi_write_then_read`
(which internally issues two back-to-back SPI transfers with CS
held low across both), unconditionally. Stock code gates this on
`SPI_CONTROLLER_HALF_DUPLEX`, which `spi-bcm2835` doesn't set.

### Patch 2 — `mcp251x_read_2regs`: always split

Same treatment for the 2-register variant. Used when the driver
reads `CANINTF` and `EFLG` atomically to handle interrupts.

### Patch 3 — `mcp251x_hw_reset`: bootstrap via CANCTRL write

After the RESET instruction and the datasheet-required oscillator
delay, explicitly writes `CANCTRL = 0x87`. That's `REQOP_CONF |
CLKEN | CLKPRE_8` — the register's normal power-up default, so
we're just writing what the chip would have written itself if RESET
had worked. The subsequent `CANSTAT` poll then sees CONFIG
reliably.

### Patch 4 — `mcp251x_hw_probe`: skip the CANCTRL sanity check

Bypasses the `(CANCTRL & 0x17) != 0x07` power-up-default check.
The CANSTAT-based CONFIG-mode verification earlier in the probe
already proves the chip is alive; the CANCTRL sanity check is
redundant and is a false negative on this board.

### Patch 5 — `mcp251x_hw_wake`: reset instead of WAKIE

Replaces the wake-from-SLEEP sequence (write CANINTE/CANINTF to
trigger the WAKIE flag, then write CANCTRL) with a full
`mcp251x_hw_reset()` call. Reasoning:

- Writing `CANCTRL` to a chip in SLEEP requires the oscillator,
  which is stopped in SLEEP. The stock wake path relies on the
  WAKIE interrupt to start the oscillator, which doesn't always
  fire on this board. The chip stays asleep, the CANCTRL write
  times out.
- The `0xC0` RESET instruction restarts the oscillator directly.
  Patched hw_reset then forces CONFIG via CANCTRL write (patch 3).
  The wake + transition to CONFIG happens in one well-defined
  sequence.

A forward declaration of `mcp251x_hw_reset` is added above
`mcp251x_hw_wake` because the file orders the wake function
first.

---

## Installing the patched module

Summarised from
[`infra/kernel-module/mcp251x-patched/README.md`](../../infra/kernel-module/mcp251x-patched/README.md).

Prerequisites (installed by
[`getting-started.md`](../getting-started.md) step 2):

```sh
pi$ sudo apt-get install -y \
      linux-headers-$(uname -r) curl xz-utils make gcc pkg-config
```

Build:

```sh
pi$ cd ~/IFS_HIL/infra/kernel-module/mcp251x-patched
pi$ ./build.sh
```

`build.sh`:

1. Fetches `mcp251x.c` from `raspberrypi/linux` branch `rpi-6.12.y`.
2. Applies `0001-backplane-hil-spi-quirks.patch`.
3. Builds as an out-of-tree module against
   `/lib/modules/$(uname -r)/build`.
4. Compresses as `.ko.xz` and installs over the stock
   `/lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz`.
5. Preserves the stock module as `mcp251x.ko.xz.orig` on first run.
6. Runs `depmod -a`.

Verify the new module carries our markers:

```sh
pi$ sudo xz -dc /lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz \
       | strings | grep -i backplane_hil
# Expected: several "Patched:" markers, "didn't wake from sleep",
#           "MCP2515 successfully initialized" strings, etc.
```

Reboot (or `modprobe -r mcp251x && modprobe mcp251x`) to pick up
the new module.

---

## Maintaining across kernel updates

The patch targets `rpi-6.12.y`. When the running kernel changes,
re-run `build.sh`:

```sh
pi$ cd ~/IFS_HIL/infra/kernel-module/mcp251x-patched
pi$ ./build.sh
```

`build.sh` always pulls the latest `mcp251x.c` from the
`rpi-6.12.y` branch. If upstream has significantly rewritten the
functions we patch, the `patch` invocation will reject. In that
case:

1. Fetch the new source by hand into `_build/mcp251x.c`.
2. Rebase the five hunks manually. Each patched region is
   decorated with a `/* Patched: ... */` comment that makes it
   easy to locate the target point in the new source.
3. Regenerate the patch against the new vanilla source:
   ```sh
   diff -u _build/.mcp251x.c.orig _build/mcp251x.c \
        > 0001-backplane-hil-spi-quirks.patch
   ```
4. Commit the updated patch.

For a major Pi OS / kernel major-version bump (e.g. 6.12.y →
7.x.y), update the `SRC_URL` in `build.sh` to the corresponding
`rpi-7.x.y` branch.

---

## Diagnostic trail (historical)

The diagnosis took longer than the implementation. Short log for
future debuggers:

- First symptom: stock overlay + stock `mcp251x` probes fail
  `-34 ERANGE`. Fixed by adding a `fixed-clock` DT node and
  referencing it via `clocks = <&mcp2515_osc>;` in the overlay.
- Next symptom: `-110 ETIMEDOUT` — "didn't enter in config mode
  after reset". Tried: slower SPI, custom pinctrl to free
  GPIO7/8, level-low vs edge-falling interrupts, cs-gpios
  polarity flip, per-device pinctrl, delayed `modprobe` after
  76 s of PSU-on, bitbanged SPI via `spi-gpio`. All same
  symptom.
- Swapped the Pi power supply to a 3 A 5VSBY unit;
  `vcgencmd get_throttled` now `0x0`. Probe **still** fails with
  -110.
- Enabled `ftrace` on the `spi/*` tracepoints during a fresh
  probe. Reads returned garbage; writes looked fine on the wire.
- Wrote a Python script that drove the chip directly via
  `/dev/spidev0.0` with `no_cs=True` + manual GPIO CS, varying
  SPI mode and transfer chunking. Discovered: split 2+1 reads in
  mode 3 round-trip correctly; single 3-byte reads don't. **That
  was the moment the driver patch direction became obvious.**
- Wrote patch 1 → probe symptom changed to
  `-19 Wrong wiring`.
- Wrote patch 3 (CANCTRL bootstrap) + 4 (skip sanity) → probe
  succeeded; canN netdevs appeared; loopback TX/RX works.
- Later: `ip link set canN up` after a `down` fails with
  `RTNETLINK Connection timed out` — wake from SLEEP. Wrote
  patch 5 → fully reliable.
- Throughout: the "inverted netdev naming" (`can0` = PCB CAN3)
  was a separate foot-gun discovered during the first end-to-end
  flash, when `discover -c can0` returned empty even though MLC1
  was powered and alive.

---

## Read these next

- [`broker-migration.md`](broker-migration.md) — why this patch
  set became a Phase 4 task.
- [`phase-history.md`](phase-history.md) — chronological order
  with PR links.
- [`../operator-guide.md`](../operator-guide.md) — how to actually
  flash a board now that all this works.
