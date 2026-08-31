# Iterating on the patched `mcp251x` module

A playbook for modifying the out-of-tree kernel module at
[`infra/kernel-module/mcp251x-patched/`](../../infra/kernel-module/mcp251x-patched/).

For the background on why the module is patched at all, see
[`../design/mcp251x-driver-patches.md`](../design/mcp251x-driver-patches.md).
This document is only about the mechanics of editing, rebuilding,
and verifying a change.

---

## When you need to touch this module

Common reasons:

- **Upstream kernel bumped the rpi-6.12.y branch in a way that
  broke our patch.** `patch` rejects, need to rebase hunks.
- **Moving to a new kernel major version** (e.g. rpi-7.0.y).
- **Adding a new hardware-specific workaround** because a PCB
  revision changed a timing margin.
- **Debugging a driver-level issue** that the stock module's log
  verbosity doesn't expose.

Uncommon reasons (avoid if possible):

- Changing CAN protocol semantics. Do that in userspace
  (python-can or the Rust flasher) — the kernel driver should
  stay close to upstream.

---

## Dev loop on the Pi

The whole cycle is:

1. Edit `mcp251x.c` inside the build tree.
2. Rebuild.
3. Install.
4. Reload the module.
5. Observe `dmesg` and retry whatever failed.

```sh
pi$ cd /home/isc/mcp251x-patched    # build.sh's persistent work dir
pi$ vim mcp251x.c
pi$ make                            # runs make -C /lib/modules/$(uname -r)/build M=$(pwd)
pi$ sudo xz -z -f mcp251x.ko
pi$ sudo cp mcp251x.ko.xz /lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz
pi$ sudo depmod -a
pi$ sudo rmmod mcp251x || true
pi$ sudo modprobe mcp251x
pi$ sudo dmesg | tail
```

The `mcp251x` module can't be `rmmod`'d while anything holds a
`canN` open. Bring the interfaces down first:

```sh
pi$ for i in 0 1 2; do sudo ip link set can$i down 2>/dev/null; done
pi$ sudo systemctl stop hil-broker    # if it's using a canN
pi$ sudo rmmod mcp251x
```

After reload:

```sh
pi$ sudo systemctl start hil-can-up hil-broker
```

---

## Where the working tree lives

`build.sh` maintains a persistent work directory at
`/home/isc/mcp251x-patched/` (or wherever the repo lives, under
`infra/kernel-module/mcp251x-patched/_build/` if you invoke it
from the repo). The work tree holds:

- `mcp251x.c` — patched source (overwritten every time
  `build.sh` fetches from upstream).
- `Makefile` — kbuild wrapper.
- `mcp251x.o`, `mcp251x.mod.o`, `.module-common.o`, `mcp251x.ko`
  — build artefacts.

Iterative edits happen **in** the work tree. Once you're happy:

1. Regenerate the patch against the upstream source:
   ```sh
   cd /home/isc/mcp251x-patched
   curl -fsSL -o mcp251x.c.orig \
       "https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.12.y/drivers/net/can/spi/mcp251x.c"
   diff -u mcp251x.c.orig mcp251x.c \
       > ~/IFS_HIL/infra/kernel-module/mcp251x-patched/0001-backplane-hil-spi-quirks.patch
   ```
2. Run the patch through `build.sh` on a fresh machine to verify
   it applies cleanly against upstream.
3. Commit.

Keep the `/* Patched: ... */` comment markers in the source —
they're the anchors you'll use to relocate hunks when upstream
rearranges the surrounding code.

---

## Debugging techniques

### Add `dev_info` / `dev_err` calls

For a quick "did this code path run?" check:

```c
dev_info(&spi->dev, "mcp251x: DEBUG reached %s:%d\n", __func__, __LINE__);
```

Rebuild, reload, watch `dmesg`. Remove before committing.

### Dynamic debug on the stock tracepoints

Before adding `dev_info`s, check if the right log lines are
already there as `dev_dbg`s (which don't print by default):

```sh
pi$ sudo cat /sys/kernel/debug/dynamic_debug/control | grep mcp251x
```

Enable one:

```sh
pi$ echo 'module mcp251x +p' | sudo tee /sys/kernel/debug/dynamic_debug/control
# or more targeted:
pi$ echo 'file mcp251x.c func mcp251x_hw_reset +p' \
       | sudo tee /sys/kernel/debug/dynamic_debug/control
```

Now the driver's `dev_dbg` lines land in `dmesg`.

### `ftrace` on SPI transfers

This is the technique that cracked the split-read quirk. Enables
kernel-level tracing on every SPI byte in and out of any device:

```sh
pi$ T=/sys/kernel/debug/tracing
pi$ echo 0 | sudo tee $T/tracing_on
pi$ sudo sh -c "> $T/trace"
pi$ echo 1 | sudo tee $T/events/spi/spi_transfer_start/enable
pi$ echo 1 | sudo tee $T/events/spi/spi_transfer_stop/enable
pi$ echo 1 | sudo tee $T/events/spi/spi_set_cs/enable
pi$ echo 1 | sudo tee $T/tracing_on

# trigger the thing you want to observe, e.g.:
pi$ sudo modprobe -r mcp251x && sudo modprobe mcp251x

pi$ echo 0 | sudo tee $T/tracing_on
pi$ sudo cat $T/trace | grep spi0
```

Every SPI transfer is one line with `tx=[xx xx xx]` and
`rx=[xx xx xx]`. Invaluable for comparing what you *think* you're
sending to what actually goes on the wire.

### `vcgencmd get_throttled`

Always check this when SPI behaviour looks random:

```sh
pi$ vcgencmd get_throttled
throttled=0x0
```

Non-zero bits = undervoltage / throttling history. See the
[troubleshooting guide](../troubleshooting.md#undervoltage-detected-in-dmesg)
for interpretation.

---

## Testing a module change

On-bench regression after any module change:

```sh
pi$ sudo dmesg | grep mcp251x   # three "successfully initialized" lines
pi$ ip -br link | grep can      # can0, can1, can2 all UP
pi$ pytest tests/hil/test_can.py -v
# All CAN tests pass; reset, init, loopback, link-health
pi$ can-flasher discover -i socketcan -c can2 --timeout-ms 3000
# If an ECU is powered on MLC1, should see node 0x01
```

If the change is substantial, run the full HIL suite to catch
any unintended regression (the CAN path is shared with a lot of
infrastructure):

```sh
pi$ pytest tests/hil/
# 93 passed, 11 skipped in ~3s
```

---

## Upstreaming a patch

Worth considering when:

- The fix is genuinely general (not specific to our PCB layout)
  and would help other users of MCP2515 on hardware with similar
  quirks.
- Our patch doesn't regress the stock-hardware behaviour.

Our patches 1 and 2 (always-split reads) could plausibly be
upstreamed as `SPI_CONTROLLER_HALF_DUPLEX`-free behaviour, but
would need more discussion about when it's safe vs. when the
performance penalty matters. Patches 3, 4, 5 are BACKPLANE_HIL-
specific workarounds; they should stay downstream.

For now, we maintain the patch out-of-tree. Low overhead, fully
under our control.

---

## Read these next

- [`../design/mcp251x-driver-patches.md`](../design/mcp251x-driver-patches.md) —
  what each patch actually does and why.
- [`setup.md`](setup.md) — general repo conventions.
- [`testing.md`](testing.md) — how to run the HIL suite after a
  module change.
