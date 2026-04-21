# Phase 4 — mcp251x kernel-driver blocker

## Status

**Parked pending hardware debug.** Phase 4 of the broker migration (see
[docs/broker_migration_plan.md](broker_migration_plan.md)) requires at
least one MCP2515 on the BACKPLANE_HIL PCB to be bound to the Linux
`mcp251x` kernel driver so the Rust
[can-flasher](https://github.com/isc-fs/can-flasher) can talk to the
ECUs over SocketCAN. Three device-tree overlay iterations on the bench
all hit the same symptom and we do not yet know the root cause.

## What works

- The register-level Python driver in [tools/mcp2515.py](../tools/mcp2515.py)
  reads CANSTAT and runs loopback tests cleanly on all three MCP2515s
  through the existing `dtoverlay=spi0-0cs` + `no_cs=True` + manual GPIO
  chip-select path.
- Both dtoverlays in [infra/devicetree/](../infra/devicetree/) compile
  with `dtc -@` without errors and load correctly at boot (kernel sees
  them, mcp251x driver binds to the declared SPI children, interrupts
  register on GPIO4/5/6 without conflict).
- `gpio=7=op,dl` at the firmware stage successfully asserts PS_ON# and
  the ATX 3V3 rail is up by the time the kernel probes — confirmed by a
  successful I2C scan finding all 7 expected devices.

## The blocker

Every probe attempt, on every MCP2515, ends with:

```
mcp251x spi0.N: MCP251x didn't enter in conf mode after reset
mcp251x spi0.N: Probe failed, err=110
mcp251x spi0.N: probe with driver mcp251x failed with error -110
```

`-110` is `-ETIMEDOUT`. The driver sequence is: send a SPI `RESET`
command (`0xC0`), then poll `CANSTAT` for the `CONFIG` bit to appear,
with a 1-second timeout. The chips never respond as expected.

We ruled out:

- **Overlay syntax**: initial `-34 -ERANGE` went away once
  `clocks = <&mcp2515_osc>;` was added (modern driver needs a clock
  node, not just `oscillator-frequency`).
- **Spidev collision**: earlier "chipselect 0/1 already in use" resolved
  by disabling `&spidev0` / `&spidev1` fragments.
- **PSU not yet up when probe runs**: we tested a delayed probe after
  ~76 s of PSU-on, `modprobe`-ing `mcp251x` explicitly. Same `-110`.
- **Crystal startup**: 76 s is >>> any crystal OST time.
- **SPI clock too fast**: tried `spi-max-frequency = <500000>`. Same.
- **Too many chips in contention**: tried both 3-chip
  ([`mcp2515-triple.dts`](../infra/devicetree/mcp2515-triple.dts)) and
  1-chip
  ([`mcp2515-can0-only.dts`](../infra/devicetree/mcp2515-can0-only.dts))
  overlays. Same on both.

## Most likely root cause

Something about how `spi-bcm2835` on the Pi 5 drives the SPI transfer
through the `cs-gpios` pins differs from what our Python driver does
with `no_cs=True` + manual GPIO writes, in a way that the MCP2515
doesn't accept. Candidates (need oscilloscope evidence):

1. **CS-to-SCLK setup time**: kernel may not hold CS low long enough
   before the first SCLK edge.
2. **SCLK idle polarity mismatch**: we set mode 0 in both paths; worth
   confirming on the wire.
3. **MISO buffer gating**: SN74LVC125A's `~OE` is driven by
   `PWR_OK → Q5 NMOS`. If the buffer's enable lags the kernel's first
   read of CANSTAT by even a few microseconds after reset, the poll
   reads floating/undefined MISO and never matches `CONFIG`.
4. **Undervoltage on the Pi itself**: dmesg reports
   `hwmon hwmon1: Undervoltage detected!` during the probe window. A
   sagging Pi 3V3 corrupts SPI signalling.

## Suggested next steps

1. **Scope SPI during probe.** Trigger on CS low on GPIO27, capture
   MOSI/MISO/SCLK. Compare two traces:
   - Broker's Python driver reading CANSTAT (known-working)
   - Kernel mcp251x's reset-then-poll sequence (failing)
2. **Fix the Pi undervoltage.** Whether or not it's the root cause, it
   shouldn't be there. Check the Pi's own 5 V supply adequacy.
3. **Also try SPI0 CE0 as the kernel's CS** (i.e. hardware CS, not
   `cs-gpios`). That would require rewiring one chip's CS from GPIO27 to
   GPIO8 for a bench test, but it isolates whether the issue is specific
   to soft CS on this combo of kernel + Pi 5.

Once probe works on at least one chip, Phase 4 proper unblocks:

- Install can-flasher via `cargo install --git
  https://github.com/isc-fs/can-flasher --tag v1.1.1` (needs
  `libudev-dev`, `pkg-config`).
- Bring up the bound interface at 500 kbit/s via `systemd-networkd`.
- Either keep the other two MCP2515s on the register-level broker
  driver (Option A) or extend the overlay back to all three (Option B).
- Adapt broker's CAN RPC methods for the kernel-bound chip (socketcan
  send/recv via `python-can`, state+counters via `pyroute2`).

## Files preserved

- [infra/devicetree/mcp2515-triple.dts](../infra/devicetree/mcp2515-triple.dts)
  — three-chip overlay (Option B).
- [infra/devicetree/mcp2515-can0-only.dts](../infra/devicetree/mcp2515-can0-only.dts)
  — single-chip overlay (Option A).

Neither is installed on the bench. The bench is running
`dtoverlay=spi0-0cs` as before.
