# Troubleshooting

Every failure mode we have genuinely seen on this bench, with the
shortest path we know to diagnose and fix it. If something fails and
isn't here, add it — document drift is the enemy.

## Quick diagnostic commands

Bookmark these; you'll run them a lot.

```sh
# Service state
systemctl is-active hil-psu-on hil-can-up hil-broker
systemctl is-active hil-bench-watchdog.timer   # the self-heal timer

# Whole bench, from ~/IFS_HIL
python3 -m tools.bench doctor                  # host build (passes on an old overlay)
python3 -m tools.bench verify --bench <id>     # hardware vs its descriptor

# GPIO state
pinctrl get 7,8                    # PS_ON, PWR_OK (comma: `get 7 8` errors)
pinctrl get 17,18,27               # MCP2515 CS pins

# CAN interfaces
ip -br link | grep can             # UP / DOWN / BUS-OFF
ip -d -s link show can2            # detailed stats + berr-counter

# Kernel messages
sudo dmesg | grep -iE 'mcp251|undervoltage'

# Broker
ls -l /run/hil-broker/broker.sock
journalctl -u hil-broker -n 40 --no-pager

# Watchdog: every check, every recovery
journalctl -u hil-bench-watchdog -n 40 --no-pager

# I²C / SPI
ls /dev/spidev0.{3..11} /dev/i2c-1  # nine spidev nodes + i2c
i2cdetect -y 1                     # apt install i2c-tools
```

---

## Recovery ladder

Two rungs clear most wedges on this bench. `hil-bench-watchdog.timer`
verifies the bench every 5 minutes and climbs them by itself when it
fails, and CI's preflight does the same before every run. To run one
by hand:

```sh
cd ~/IFS_HIL
python3 -m tools.bench recover --bench <id> --level 1   # restart hil-broker
python3 -m tools.bench recover --bench <id> --level 2   # PSU power-on reset, reload
                                                        # mcp251x, restart hil-can-up
                                                        # + hil-broker
```

`recover` waits for the bench lock (`/tmp/hil-bench.lock`), so it never
cycles the rails under a flash. The watchdog takes the same lock
non-blocking and leaves a busy bench alone. Before it recovers, it
appends the wedged state (DAC device ids, PSU status, CAN links) to
`~/hil-wedge-evidence.jsonl` — the power-on reset erases it. It logs
healthy checks too: a bench that needs recovering on every cycle is a
worsening fault, not a fixed one.

If level 2 doesn't clear it, the watchdog logs `STILL unhealthy after
level 2 — needs a human` and CI's preflight fails with `bench still
does not match its descriptor after recovery`. Run
`python3 -m tools.bench verify --bench <id>` and fix what it names.

---

## By symptom

### `mcp251x spiN: probe with driver mcp251x failed with error -110`

`-ETIMEDOUT` during the probe sequence. Root cause tree:

1. **Running the stock unpatched `mcp251x` module.**
   ```sh
   M=/lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz
   sudo md5sum "$M" "$M.orig"
   ```
   The two hashes must **differ**. The same hash, or no `.orig`, means
   you're on the stock module (no `.orig` = never built for this
   kernel). Run
   [`infra/kernel-module/mcp251x-patched/build.sh`](../infra/kernel-module/mcp251x-patched/)
   and reboot. Don't grep the binary for `backplane_hil` instead: the
   patch's markers are C comments, stripped at compile time, so that
   check fails on a correctly patched bench too.

2. **PSU not on when the kernel probed.** The chips are on +3V3
   main; if `PS_ON#` wasn't asserted at firmware stage, the probe
   hits a powered-off chip.
   ```sh
   grep -E '^gpio=' /boot/firmware/config.txt
   # Expected:
   #   gpio=7=op,dl
   #   gpio=8=ip,pd
   ```
   Missing? Add them and reboot.

3. **Chip in a bad state from a previous session.** The ATX PSU
   stays on across Pi reboots because firmware re-asserts GPIO7,
   so chip state can persist. Force a real power cycle —
   `python3 -m tools.bench recover --bench <id> --level 2` does it
   through the broker; with the broker down, by hand:
   ```sh
   sudo pinctrl set 7 op dh    # PSU off
   sleep 2
   sudo pinctrl set 7 op dl    # PSU on
   sudo modprobe -r mcp251x
   sudo modprobe mcp251x
   sudo systemctl restart hil-can-up hil-broker
   ```

4. **Undervoltage on the Pi.** Check `sudo dmesg | grep -i undervoltage`
   and `vcgencmd get_throttled` (non-zero = problem). Pi 4 needs
   ≥ 3 A on its 5 V input. Common trap: powering the Pi from the
   ATX 5V standby rail with a PSU that only supplies ~2 A there.
   Use a dedicated 5 V / 3 A supply or route through a beefier rail.

### `mcp251x spiN: Cannot initialize MCP2515. Wrong wiring?`

`-ENODEV` at the CANCTRL sanity-check step. Our patches bypass this
check because the CANCTRL register reads unreliably on this board.
If you see this error, the patched module is **not** active.
See the stock-module check above.

### `RTNETLINK answers: Connection timed out` on `ip link set canN up`

The ndo_open path calls the driver's wake-from-sleep, which fails
if the chip isn't actually alive. Same remediation tree as the
-110 probe failure. Often means PSU isn't on.

```sh
pinctrl get 7    # must show "op .. lo"
pinctrl get 8    # must show "ip .. hi"
```

### `can-flasher discover` returns "No bootloaders replied"

Three things in order (`flash_dut` reports this as `no bootloader
answered`):

1. **Wrong channel.** The MLC carriers are on PCB CAN1 = kernel
   `can2`. Not `can0`. See
   [`hardware-reference.md`](hardware-reference.md#can-netdev--pcb-label-mapping-crucial).

2. **Carrier not powered.**
   ```python
   c.call('ina.current', addr=0x40) * 1000   # MLC1 → mA
   ```
   Should be ~130 mA for a running bootloader. ≤ 1 mA means the
   relay didn't close or the carrier fuse is blown (F5–F14; `flash_dut`
   says `drew only … mA — carrier not seated, or fuse blown`). Make the
   TCA port outputs first (`tca.set_direction`, mask `0x00`): on a
   freshly reset expander, `tca.write_pin` alone is a silent no-op.

3. **App is already running.** `flash --jump` gave the target to
   the app, which doesn't listen on BL CAN IDs. Send it the DUT's
   boot trigger (`bl_trigger_payload` in its profile); the app
   reboots into its bootloader:
   ```sh
   cansend can2 002#B007AD12    # ECU
   cansend can2 002#B007AD11    # AMS
   ```
   Right after powering a carrier, wait until the app is talking
   (`candump -n 1 can2`) — bench-01's ECU takes ~2 s to its first
   frame, and a trigger sent earlier is lost. (`can-flasher …
   send-raw 0x001 03 06 01` is the *demo* firmware's trigger, not
   the ECU's or the AMS's.) `tools.flash_dut` does all of this for
   you.

### `can-flasher discover` shows two rows with the same node ID

Multiple bootloaders on the same bus answering the same node ID (e.g.
both still at the factory default `0x01`), colliding during ISO-TP
reassembly. You'll also see `NoFirstFrame` or `BadSeq` warnings. Ids
are meant to follow ECU `0x01` / AMS `0x02` / uDV `0x03`, but each
lives in the carrier's bootloader NVM and can drift — bench-01's AMS
answered `0x01` until it was re-provisioned on 2026-09-25.

- **Quick fix**: power one carrier at a time via the dashboard
  or `tca.write_pin`. `flash_dut` always does: it de-energises every
  other DUT slot, and refuses to flash if more than one node answers.
- **Permanent fix**: provision distinct node IDs with
  `can-flasher ... config --set node-id 0xN` per board.

### `discover` lists the node, but `flash` fails `CONNECT failed: timed out`

`--node-id` doesn't match the id this carrier's bootloader answers to.
`discover` is a broadcast, so it lists the node regardless; `flash`
talks only to the id you give it. bench-01's CI flash of 2026-09-25
failed exactly like this: its AMS had just been re-provisioned to
`0x02` while the profile still said `0x01`. Use the id `discover`
printed. For `flash_dut` and the DUT suites, `bl_node_id` in the DUT
profile (`tests/hil/vcu/vcu_profile.yaml`,
`tests/hil/ams/ams_profile.yaml`) must match the carrier.

### `flash failed ... No buffer space available (os error 105)` during flash

`ENOBUFS` from socketcan. The canN `txqueuelen` is too small for
sustained flash writes.

```sh
ip -o link show can2 | grep -oE 'qlen [0-9]+'
# Expected: qlen 1000
```

If it's 10, `hil-can-up.service` didn't run — or ran before a
`mcp251x` reload recreated the interfaces. (Re)start it:
```sh
sudo systemctl restart hil-can-up
```

Or fix the interface manually for the current session:
```sh
sudo ip link set can2 down
sudo ip link set can2 txqueuelen 1000
sudo ip link set can2 up type can bitrate 500000 sample-point 0.6875 restart-ms 200
```

Always pass `sample-point 0.6875`: without it the kernel picks 0.875
for the `mcp251x` at 500 k, and the STM32 DUTs bus-off — which reads
as a firmware fault. Either way this reconfigures `can2`, which can
wedge the `mcp251x` (see below) — never do it with a carrier under
test.

### `flash failed ... session RX task exited — backend may have disconnected: device not found / timeout`

The canN interface went DOWN mid-flash. Causes seen in the wild:

- Momentary bus-off (chip got overwhelmed by errors). With
  `restart-ms=200` the kernel auto-recovers; just retry. The
  flasher's `--verify-after` + idempotent erase-skip-writes mean
  retry is safe.
- Something else on the Pi brought canN down. Check
  `journalctl -u hil-can-up -b`.

Flash by hand under `flock /tmp/hil-bench.lock`, as CI does: the
watchdog leaves a locked bench alone, but recovers an unlocked one that
fails its checks — and level 2 cycles the PSU.

### `BAD_SESSION` mid-flash, carrier left with no app

`can-flasher` NACKs part-way through the image — after the erase, so
the carrier is left appless. Before 2.8.0 one dropped frame killed the
transfer (no ISO-TP session recovery, MingoCAN#506); 2.8.0 and later
still do it occasionally. Retry before suspecting the firmware: the
bootloader stays reachable. An appless carrier shows `no app
installed` in `discover` and so has no product string to check;
`flash_dut` says so and proceeds on slot isolation alone.

### `flash_dut`: `can-flasher X is too old to flash reliably; need >= 2.8.0`

Refused before anything is energised or erased, on purpose: builds
before 2.8.0 erase, then fail mid-image and leave the carrier with no
app (2.5.5 wiped bench-01's AMS that way). Install a current release
from `isc-fs/MingoCAN` —
[`getting-started.md` §10](getting-started.md#10-install-can-flasher).

### Dashboard shows everything as red / not responding

Broker isn't running, or can't reach hardware.

```sh
systemctl status hil-broker
ls -l /run/hil-broker/broker.sock
# socket present = broker up
```

If the socket's missing:
```sh
journalctl -u hil-broker -b -n 50 --no-pager
```

Common culprits in the log: Python import error (broken `pip
install -e '.[bench]'` — plain `-e .` leaves out the Pi-only
`spidev` / `smbus2` / `RPi.GPIO` the real backend needs), SPI/I²C
permission error (user not in `spi`/`i2c` groups, or `pip install`
failed to reach sudo), `/dev/spidev0.3` missing (see below).

### `Address already in use` on port 8080

Another dashboard instance is still running:
```sh
pkill -f dashboard/app.py
sudo systemctl restart hil-dashboard
```

### `pytest tests/hil/` all skipped

The HIL fixtures skip everything when they can't reach the broker.
Usually because `HIL_BROKER_SOCKET` isn't set to the right path
and the default `/run/hil-broker/broker.sock` isn't there (e.g.
you started the broker manually at `/tmp/hil-broker.sock`):

```sh
export HIL_BROKER_SOCKET=/tmp/hil-broker.sock
pytest tests/hil/ --ignore=tests/hil/vcu --ignore=tests/hil/ams
```

Or run under the systemd-managed socket:
```sh
unset HIL_BROKER_SOCKET
pytest tests/hil/ --ignore=tests/hil/vcu --ignore=tests/hil/ams
```

The `--ignore`s keep this to the bench self-tests: `tests/hil/vcu/`
(the ECU) and `tests/hil/ams/` are DUT suites that can reflash a
carrier — see the next entry before running them.

### A hand-run DUT suite reports on the wrong firmware

Block A's A-003 **reflashes** the carrier from `ECU_FIRMWARE_BIN` /
`AMS_FIRMWARE_BIN`. On a hand run with `ECU_FIRMWARE_BIN` unset it
falls back to `~/firmware-builds/ECU_fix.bin` — a stale 2026-06
diagnostic build that exists on bench-01 — and every later case then
reports on that, while A-003 itself passes. (The AMS falls back to
`/tmp/AMS.bin`.) Point it at your image, and take the bench lock so a
dispatched run can't land mid-session:

```sh
export ECU_FIRMWARE_BIN=/path/to/ECU.bin
flock /tmp/hil-bench.lock \
  pytest $(python3 -m tools.bench suite --dut ecu --suite smoke) -v
```

CI sets both variables; there, an unset one fails the run instead of
falling back.

### `pytest tests/broker/` fails with `ModuleNotFoundError: broker.fake_bus`

pytest is running from a directory that doesn't have the repo root
on `sys.path`. Run from the repo root:
```sh
cd ~/IFS_HIL
pytest tests/broker/
```

### `ip link set canN up` succeeds but `candump` shows nothing

Most likely the chip is alive but nothing else on the bus is
transmitting. If you expect traffic, check:

- Is the relay for the transmitting carrier energised?
- Is the far-end ECU actually running (INA226 current > 100 mA)?
- Is the far-end speaking at the same bitrate (500 kbit/s)?

Put the chip in loopback mode and `cansend` + `candump` to prove
the kernel path works:
```sh
sudo ip link set can2 down
sudo ip link set can2 up type can bitrate 500000 loopback on
candump can2 &
cansend can2 123#DEADBEEF
```

Sent frame should come right back on the same interface. Then put
the link back (`kill %1; sudo systemctl restart hil-can-up hil-broker`),
and don't do this with a carrier under test: reconfiguring `can2` can
wedge the `mcp251x` (next entry).

### `can2` traffic stops, but `ip link` still says UP / ERROR-ACTIVE

The `mcp251x` driver has wedged. It looks exactly like the firmware
stopped transmitting — the link state is fine, the frames just stop —
and has been mistaken for a bricked board. It follows reconfiguring
`can2` (`ip link set can2 …`) or a PSU cycle, which resets the
MCP2515s while the kernel still believes the links are up. Reload the
module and bring the links back:

```sh
sudo modprobe -r mcp251x && sudo modprobe mcp251x
sudo systemctl restart hil-can-up hil-broker
```

`recover --level 2` does the same after a PSU power-on reset. Rule
this out before believing a DUT is dead. After a PSU cycle by hand
(`psu.power(False)` / `(True)`), run these two lines too — restarting
only the broker leaves CAN silent.

### A DAC returns the wrong device id (expected `0x0417`)

`bench verify` reports `DAC n does not return a valid device id
(drives: …)`, and whatever that DAC drives is dead with it — on
bench-01, DAC 0 is the ECU's brake/APPS stimulus and DAC 3 the AMS
pack current. `0x0000` and `0x3FFF` are the floating-low/high
patterns. A one-off `0x082E` (`0x0417` shifted one bit) is a
mis-clocked read under CAN load, which `verify` retries away.

The cause was a userspace chip-select racing the kernel `mcp251x`
driver on the shared SPI controller (#124), fixed by giving every
chip-select to the kernel. A DAC that stays wrong needs a real
power-on reset — a broker restart only reopens the SPI handle:

```sh
python3 -m tools.bench recover --bench <id> --level 2
```

The watchdog does this unattended. If it keeps coming back, check
that `/dev/spidev0.4`–`0.7` exist (below): an old overlay puts the
DACs back on userspace chip-selects.

### `undervoltage detected!` in dmesg

The Pi's input voltage dipped below ~4.63 V. SPI signalling gets
unreliable; probes fail, reads corrupt. Get a proper 5 V / 3 A
supply for the Pi (do not rely on the ATX 5VSBY rail unless it's
explicitly rated at 3 A+).

### `BUS-OFF` state sticky

`hil-can-up.service` sets `restart-ms=200`, so bus-off should
self-recover. If an interface is stuck:

```sh
ip -d link show can2 | grep -E 'state|restart-ms'
```

`restart-ms 0` means auto-recovery isn't configured (the service
didn't run, or the link was reconfigured by hand since). Restart it:
```sh
sudo systemctl restart hil-can-up
```

Or fix for the session:
```sh
sudo ip link set can2 down
sudo ip link set can2 up type can bitrate 500000 sample-point 0.6875 restart-ms 200
```

If even then it re-enters bus-off immediately, you have a real
bus problem: no peer, bitrate mismatch with the peer, termination
wrong, or a short on the bus wires — or a sample-point mismatch:
`ip -d link show can2` must say `sample-point 0.687`. `hil-can-up`
refuses to come up without it (`journalctl -u hil-can-up -b` shows
`canN: sample-point is not 0.6875 — DUTs will bus-off`).

### `sudo -n ip link set ...` fails with `a password is required`

The sudoers drop-in isn't installed or the user isn't `isc`:
```sh
ls -l /etc/sudoers.d/hil-broker    # expected: -r--r----- root:root
sudo visudo -c                     # must say parsed OK
```

If you run the broker under a different username, edit the
sudoers file accordingly.

### `/dev/spidev0.4`–`0.11` missing, `/dev/spidev0.3` present

An **old overlay**, from before the kernel took over every
chip-select (2026-09-04, #124). The current one gives each device
its own node — `spidev0.4`–`0.7` the four DAC80504s, `0.8`–`0.10`
the three MCP3208s, `0.11` the nRF24 — while `spi0.0`–`0.2` are the
MCP2515s under `mcp251x`, with no spidev nodes (don't add any).
`spidev0.3` is the legacy shared node.

On an old overlay the bench still comes up: the broker falls back
to `spidev0.3` with userspace chip-selects — the DAC-wedge race —
and logs `per-DAC spidev nodes missing` / `per-ADC spidev nodes
missing` instead of `CS owned by the kernel`. `bench doctor` only
looks for `spidev0.3`, and `bench_setup.sh` only checks that *a*
`.dtbo` is installed, so both pass. Check directly:

```sh
ls /dev/spidev0.{3..11}          # all nine
journalctl -u hil-broker -b | grep -E 'spidev nodes missing|CS owned by the kernel'
```

Recompile and reinstall the overlay, then reboot:
```sh
cd ~/IFS_HIL
dtc -@ -I dts -O dtb -o infra/devicetree/mcp2515-triple.dtbo \
    infra/devicetree/mcp2515-triple.dts
sudo cp infra/devicetree/mcp2515-triple.dtbo /boot/firmware/overlays/
sudo reboot
```

### `/dev/spidev0.3` does not exist

The `mcp2515-triple` overlay isn't loaded (the broker can't start
without that node):

```sh
grep dtoverlay /boot/firmware/config.txt
```

Expected: `dtoverlay=mcp2515-triple` (not `spi0-0cs`). If it's
still `spi0-0cs`, edit config.txt and reboot. If it's
`mcp2515-triple` but the device still doesn't exist, the overlay
failed to load — check `sudo dmesg | grep -i 'overlay\|spi0'`.

### `I2C devices: []` in a scan

Either the I²C bus isn't enabled or all chips are off.

```sh
sudo raspi-config nonint do_i2c 0     # enable I²C and reboot
ls /dev/i2c-*                         # expected: /dev/i2c-1
i2cdetect -y 1
```

Note INA226 and TCA9555 are on +3V3SBY, so they answer even with
the PSU off. If I²C enumerates zero chips, the problem is the Pi
side (I²C disabled, cable disconnected, bad pull-ups on a bodged
board), not the PSU.

### Pi SSH drops during CAN traffic

If you're over a VPN, that's your problem — not the Pi. Verified
by `systemctl status` showing uptime unchanged after the drop.
Try a direct LAN connection for high-throughput work.

If the Pi itself did reboot (uptime reset), suspect undervoltage
or a power blip from relay kick-back. Add a larger bulk cap on
the +12V relay rail, or re-verify flyback diodes D1–D4 are
populated.

### Watchdog logs `RUNNER DOWN`, or dispatched runs sit in Queued, then time out

The self-hosted runner service isn't active. The hardware can be
perfectly healthy while every dispatched run queues against an offline
runner: bench-01 lost a week to this from 2026-09-18, with the watchdog
reporting `healthy` throughout. It reports the runner now, but never
restarts it. (`resolve` fails fast with `No online self-hosted runner
carries all of: …` when its token may list runners; otherwise the test
job just queues.)

```sh
U=$(systemctl list-unit-files 'actions.runner.*.service' --no-legend | awk '{print $1}')
systemctl status "$U" --no-pager
journalctl -u "$U" -n 50 --no-pager
systemctl show -p Restart --value "$U"    # must print: always
sudo systemctl start "$U"
```

If `Restart` isn't `always`, the drop-in is missing and a runner that
exits is never brought back: install
`infra/systemd/actions.runner.restart.conf` per
[`infra/systemd/README.md`](../infra/systemd/README.md), or re-run
`scripts/bench_setup.sh --bench <id>`. `bench doctor` §14 checks both
the drop-in and that the unit is enabled. Re-register the runner only
if GitHub really deleted the registration: bench-01's "registration
has been deleted" was transient, and a plain restart brought it back.

### Firmware PR looks green, but no HIL verdict ever arrives

On this bench, silence is not success. A workflow with an invalid
`${{ }}` expression fails at *startup* — zero jobs, no check-run — so
`gh pr checks` reads green; that hid a dead chain for 23 days
(2026-09-02 → 2026-09-25). Check that the run actually has jobs:

```sh
gh run list -R isc-fs/IFS_HIL -w hil-test.yml -L 5
gh api repos/isc-fs/IFS_HIL/actions/runs/<id>/jobs --jq .total_count   # must be > 0
```

A run whose jobs sit in Queued is the runner case above. No run at
all means nothing was dispatched: check the firmware repo's own
trigger run, remember that a `/hil-test` *comment* runs the trigger
from the firmware repo's default branch (a label uses the PR's own
copy), and check the two tokens — `HIL` dispatches,
`HIL_CROSS_REPO_PAT` checks the firmware out and posts the verdict
([`HANDOVER.md` §5](../HANDOVER.md#5-ci--the-fleet-model)). `/hil-build`
is the dead legacy chain: no runner carries its `hil-rpi` label, so
it only queues.

---

## Logs to gather before asking for help

When opening an Issue or asking in team chat:

```sh
sudo dmesg | tail -60 > /tmp/dmesg.txt
journalctl -u hil-psu-on -u hil-can-up -u hil-broker -b \
    --no-pager > /tmp/services.txt
journalctl -u hil-bench-watchdog -b --no-pager > /tmp/watchdog.txt
cp ~/hil-wedge-evidence.jsonl /tmp/ 2>/dev/null   # state captured before each recovery
for i in 0 1 2; do ip -s -d link show can$i; done > /tmp/can.txt
pinctrl get 4-12 > /tmp/gpio.txt
systemctl list-unit-files --state=enabled | grep hil- > /tmp/units.txt
vcgencmd get_throttled > /tmp/throttle.txt
```

Attach the `/tmp/*.txt` files (and the `.jsonl`, if there is one) or
paste the relevant bits inline.
Having all of these up front is usually enough to diagnose
anything you'll hit.
