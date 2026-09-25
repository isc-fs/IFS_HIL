# Iterating on the patched `mcp251x` module

A playbook for modifying the out-of-tree kernel module at
[`infra/kernel-module/mcp251x-patched/`](../../infra/kernel-module/mcp251x-patched/).

For the background on why the module is patched at all, see
[`../design/mcp251x-driver-patches.md`](../design/mcp251x-driver-patches.md).
This document is only about the mechanics of editing, rebuilding,
and verifying a change.

---

## When you need to touch this module

A kernel upgrade alone doesn't need an edit: `build.sh` builds
against `$(uname -r)` and installs into that kernel's module tree,
so after an upgrade just re-run it (until you do, the new kernel
loads its stock `mcp251x` and the probes fail).

Common reasons to edit:

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

First keep the watchdog and CI off the bench for the session:

```sh
pi$ sudo systemctl stop hil-bench-watchdog.timer
pi$ flock /tmp/hil-bench.lock bash      # work in this shell; `exit` releases the lock
```

The watchdog runs every 5 min. A missing or misconfigured `can2`
looks like a wedge to it, and its recovery ladder reloads `mcp251x`
and power-cycles the rails under you; each tick also starts
`hil-broker` — and through its `Wants=`, `hil-can-up` — if you stopped
them. The lock keeps CI flashes off the bench. Inside that shell,
don't nest another `flock` on the same file or run
`python3 -m tools.bench recover`: both wait for the lock you hold.
When you're done, `exit` and `sudo systemctl start
hil-bench-watchdog.timer`.

```sh
pi$ cd ~/IFS_HIL/infra/kernel-module/mcp251x-patched/_build   # build.sh's work dir (run ./build.sh once first)
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

After reload — `restart`, not `start`: `hil-can-up` is a oneshot
that stays "active" after it runs, so `start` would do nothing and
leave the new interfaces down:

```sh
pi$ sudo systemctl restart hil-can-up hil-broker
```

---

## Where the working tree lives

`build.sh` works in `_build/` next to itself — on the bench,
`~/IFS_HIL/infra/kernel-module/mcp251x-patched/_build/`. It is not
persistent: every run deletes `_build/`, copies in the `Makefile`,
fetches `mcp251x.c` fresh from `rpi-6.12.y` and applies the patch.
The work tree holds:

- `mcp251x.c` — patched source (replaced on every `build.sh` run).
- `Makefile` — kbuild wrapper.
- `mcp251x.o`, `mcp251x.mod.o`, `.module-common.o`, `mcp251x.ko`
  — build artefacts.

Iterative edits happen **in** the work tree, and the next
`build.sh` run throws them away — so turn them into the patch
first. Once you're happy:

1. Regenerate the patch against the upstream source, with the
   `a/` / `b/` names that `build.sh`'s `patch -p1` expects:
   ```sh
   cd ~/IFS_HIL/infra/kernel-module/mcp251x-patched
   curl -fsSL -o /tmp/mcp251x.c.orig \
       "https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.12.y/drivers/net/can/spi/mcp251x.c"
   diff -u --label a/mcp251x.c --label b/mcp251x.c \
       /tmp/mcp251x.c.orig _build/mcp251x.c \
       > 0001-backplane-hil-spi-quirks.patch
   ```
   This drops the file's three `#` header lines; `patch` skips
   leading text, so restore them by hand if you want them.
2. Run `./build.sh` again — it starts from a fresh upstream copy,
   so it proves the patch applies cleanly.
3. Copy the patch back to your workstation's git checkout and
   commit it there. The bench copy is rsync-managed, and the next
   `scripts/sync_to_pi.sh` would overwrite it with the old patch.

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

On-bench regression after any module change, still in the locked
shell from the dev loop:

```sh
pi$ M=/lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz
pi$ sudo md5sum "$M" "$M.orig"  # must DIFFER: the patched module is installed
pi$ sudo dmesg | grep mcp251x   # three "successfully initialized" lines
pi$ ip -br link | grep can      # can0, can1, can2 all UP
pi$ pytest tests/hil/test_can.py -v
# All CAN tests pass; reset, init, loopback, link-health
pi$ sudo systemctl restart hil-can-up   # test_can.py leaves the links down
pi$ can-flasher discover -i socketcan -c can2 --timeout-ms 3000
# A powered carrier sitting in its bootloader answers: ECU 0x01, AMS 0x02
```

Don't verify by grepping the binary for the `/* Patched: … */`
markers — they are C comments, stripped at compile time, so that
check fails on a correctly built module.

If the change is substantial, run the bench self-tests to catch
any unintended regression (the CAN path is shared with a lot of
infrastructure), then restore the bench and prove a real flash:

```sh
pi$ pytest tests/hil/ --ignore=tests/hil/vcu --ignore=tests/hil/ams
# 93 passed, 11 skipped in ~3s
pi$ sudo systemctl restart hil-can-up hil-broker
pi$ python3 -m tools.flash_dut --dut ams --bin /path/to/AMS.bin   # or --dut ecu
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
