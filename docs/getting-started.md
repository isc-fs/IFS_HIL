# Getting started — from a blank Raspberry Pi to flashing an ECU

This is the reproducible path from a freshly-imaged Raspberry Pi 4 to
a working HIL bench that can discover and flash a bootloader-equipped
STM32 over CAN. Every step is a prerequisite for the next; skipping
ahead is an easy way to burn an afternoon debugging.

Plan on **45 minutes** end-to-end if nothing goes sideways, plus one
reboot.

> **The automated path is [`scripts/bench_setup.sh`](../scripts/bench_setup.sh).**
> It performs sections 1–10 and 14 of this guide for you — resumably,
> stopping at the reboot and at the descriptor it needs you to fill in:
>
> ```sh
> pi$ scripts/bench_setup.sh --bench bench-02 --dry-run   # report only
> pi$ scripts/bench_setup.sh --bench bench-02             # do it; re-run after the reboot
> ```
>
> This guide is the explain-every-step version. Read it once, so you can
> debug the script when it stops. What neither covers (CI credentials,
> stimulus hardware, network) is listed in
> [`HANDOVER.md`](../HANDOVER.md) §4.

---

## 0. Prerequisites

### Hardware

- **Raspberry Pi 4 Model B**, 2 GB or more, with a working Ethernet or
  Wi-Fi connection and SSH enabled.
- **BACKPLANE_HIL PCB**, populated per the design review
  (`docs/BACKPLANE_HIL/design_review.md`, not tracked in the repo —
  ask the team for the KiCad project).
- **ATX power supply** with **≥ 3 A on +5 V standby**. Lower-rated
  supplies cause Pi undervoltage events that break SPI signalling.
  A consumer-grade desktop PSU usually has 2 A SBY; server-grade or
  a dedicated 5 V / 3 A brick works.
- **An STM32H733ZG carrier board** flashed with the
  [isc-fs/stm32-can-bootloader](https://github.com/isc-fs/stm32-can-bootloader)
  image. This is assumed to be burned via SWD out-of-band; the HIL
  bench does not burn the bootloader itself.
- **Ethernet cable** or a reachable Wi-Fi SSID; the bench doesn't do
  headless provisioning from nothing.

### Software

- **Raspberry Pi OS Lite (64-bit)**, Bookworm or newer. This guide was
  validated against `6.12.47+rpt-rpi-v8`.
- An SSH client on your workstation.
- A GitHub account with access to
  [`isc-fs/MingoCAN`](https://github.com/isc-fs/MingoCAN)
  (private repo — you'll need `gh auth login` on the downloading host
  at install time).

### Conventions

Commands prefixed with `$` run on your workstation. Commands prefixed
with `pi$` run on the Pi over SSH. Commands without a prefix can run
anywhere that context is obvious from the surrounding prose.

The default Pi user in this guide is `isc`. If yours differs, adjust
the sudoers file and systemd units accordingly.

---

## 1. Pi OS configuration

Enable I²C:

```sh
pi$ sudo raspi-config nonint do_i2c 0
```

You don't need to enable SPI through `raspi-config`: the
`mcp2515-triple` overlay (section 4) provides SPI0 itself. The
known-good bench-01 runs without `dtparam=spi=on` — `raspi-config
nonint get_spi` reports "disabled" there while every spidev node works —
so leave it out rather than add a second claimant for SPI0.

> ⚠️ Unverified on a fresh image: `bench_setup.sh` enables SPI with
> `do_spi` when it finds neither the overlay nor `dtparam=spi=on` — which
> is always the case on a fresh Pi, because it installs the overlay later
> (section 4). A from-scratch run may therefore end with *both* lines in
> `config.txt`. bench-01 was built by hand and never took that path; if you
> are doing the first scripted bringup, check `config.txt` afterwards.

Add your user to the hardware groups. `spi`, `i2c`, and `gpio` are
required for direct device access; `dialout` is needed if you ever
plug in a CANable or ST-Link over USB.

```sh
pi$ sudo usermod -aG spi,i2c,gpio,dialout,netdev isc
```

Log out and back in for the groups to take effect.

---

## 2. Install system packages

```sh
pi$ sudo apt-get update
pi$ sudo apt-get install -y \
      python3-can \
      can-utils \
      device-tree-compiler \
      xz-utils \
      libudev-dev \
      pkg-config \
      linux-headers-$(uname -r) \
      gcc-arm-none-eabi cmake \
      git curl
```

- `python3-can` — SocketCAN backend for the broker.
- `can-utils` — `cansend`, `candump`, `cangen` for manual bus work.
- `device-tree-compiler` — needed once to compile our `.dts` overlay.
- `xz-utils` + `linux-headers-$(uname -r)` — needed to build the
  patched `mcp251x` kernel module.
- `gcc-arm-none-eabi` + `cmake` — for CI's artifact fallback: when a
  cloud build can't hand its artifact to the bench (a full GitHub
  artifact quota is the known case), `hil-test.yml` rebuilds the same
  reviewed commit on the bench instead.

---

## 3. Clone the repo

The repository is public, so this needs no credentials on the Pi:

```sh
pi$ git clone https://github.com/isc-fs/IFS_HIL.git
pi$ cd IFS_HIL
pi$ git checkout dev
```

This is the only time you touch git on the bench. From here on,
update `~/IFS_HIL` — the tree the services run from — from a developer
machine with `scripts/sync_to_pi.sh` (see
[`CLAUDE.md`](../CLAUDE.md#pi-sync-workflow-read-before-pushing-code-to-the-bench)),
which also lets you test uncommitted work. Don't mix in `git pull`
there, and never sync with `--delete`.

Install Python dependencies in editable mode so the `tools.*` and
`broker.*` packages resolve from your working copy:

```sh
pi$ pip install -e '.[bench]' --break-system-packages
```

The `[bench]` extra pulls in the Pi-only hardware drivers (`spidev`,
`smbus2`, `RPi.GPIO`). Plain `pip install -e .` deliberately omits them
so the repo stays installable on a developer laptop; on a bench you want
the extra, or the real broker backend cannot open the buses.

(The `--break-system-packages` flag is Pi OS Bookworm's opt-in for
system-wide `pip install`. If you prefer a venv, create one in
`~/IFS_HIL/.venv`, activate it, and drop the flag.)

---

## 4. Device-tree overlay

The BACKPLANE_HIL wires three MCP2515 CAN controllers onto a shared
SPI0 bus with chip-selects on GPIO27 (CAN1), GPIO17 (CAN2), GPIO18
(CAN3), and interrupts on GPIO4/5/6. The same bus carries the four
DACs, three ADCs and the nRF24, each on its own chip-select. No stock
Raspberry Pi overlay covers this; we ship a custom one.

Compile and install the overlay:

```sh
pi$ cd ~/IFS_HIL
pi$ dtc -@ -I dts -O dtb \
       -o infra/devicetree/mcp2515-triple.dtbo \
          infra/devicetree/mcp2515-triple.dts
pi$ sudo cp infra/devicetree/mcp2515-triple.dtbo \
            /boot/firmware/overlays/
```

Edit `/boot/firmware/config.txt`:

```sh
pi$ sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.pre-hil
pi$ sudo nano /boot/firmware/config.txt
```

Replace the line `dtoverlay=spi0-0cs` with the block:

```
dtoverlay=mcp2515-triple
gpio=7=op,dl
gpio=8=ip,pd
```

Why each entry:

- `dtoverlay=mcp2515-triple` — wires the three MCP2515s and declares
  **all twelve SPI0 chip-selects as `cs-gpios`**, so the kernel asserts
  every one of them inside its transfer: the CAN chips on
  `spi0.0`–`0.2`, the DACs on `/dev/spidev0.4`–`0.7`, the ADCs on
  `0.8`–`0.10` and the nRF24 on `0.11`, plus the legacy shared
  `/dev/spidev0.3`. Userspace-driven chip-selects raced the CAN driver
  and wedged the DACs (#124); this is the fix.
- `gpio=7=op,dl` — asserts `PS_ON#` LOW at firmware stage so the ATX
  main rails are stable before the kernel probes the CAN chips.
- `gpio=8=ip,pd` — forces `PWR_OK` back to pulled-down input so it
  reads correctly (overrides the default SPI0_CE0 pinmux).

Keep `/boot/firmware/config.txt.pre-hil` as your rollback image in
case the next reboot doesn't come up.

> **Refreshing an existing bench:** `bench_setup.sh` only checks that
> *a* `mcp2515-triple.dtbo` is installed, and `bench doctor` only looks
> for `/dev/spidev0.3` — neither notices an old overlay. Recompile and
> copy the `.dtbo` by hand, reboot, and check section 8's spidev list.

---

## 5. Patched `mcp251x` kernel module

The stock `mcp251x` driver does not probe on this hardware because of
three hardware-level quirks (see
[`docs/design/mcp251x-driver-patches.md`](design/mcp251x-driver-patches.md)
for why). Our out-of-tree build fixes them.

```sh
pi$ cd ~/IFS_HIL/infra/kernel-module/mcp251x-patched
pi$ ./build.sh
```

The script fetches the matching upstream source, applies our patch,
builds against the running kernel's headers, compresses the module,
and installs it at
`/lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz`.
The stock module is preserved at `mcp251x.ko.xz.orig` for rollback.

Verify the patched module is the one installed:

```sh
pi$ M=/lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz
pi$ sudo md5sum "$M" "$M.orig"
# Expected: two DIFFERENT hashes. Same hash (or no .orig) means the
# patch never landed and the stock module is still in place.
```

> Earlier revisions of this guide said to run
> `xz -dc … | strings | grep -i backplane_hil` and expect
> `/* Patched: … */` markers. That check can never pass: those markers are C
> *comments*, which the compiler strips — they are not in the binary. It
> reported failure on a correctly built bench and sent people to
> troubleshooting for no reason.

---

## 6. Install the sudoers drop-in

The broker runs as the `isc` user. Managing `canN` link state
(`ip link set can0 up …`) needs `CAP_NET_ADMIN`. We grant the narrow
escalation via `sudo -n`:

```sh
pi$ cd ~/IFS_HIL
pi$ sudo cp infra/sudoers.d/hil-broker /etc/sudoers.d/hil-broker
pi$ sudo chmod 0440 /etc/sudoers.d/hil-broker
pi$ sudo visudo -c    # parse-check; "parsed OK"
```

Only `ip link set canN …` is allowed; no other escalation is granted.

---

## 7. Install systemd units

These units manage the bench at boot, in this order:

1. `hil-psu-on.service` — asserts `PS_ON#` in userspace (complements
   the firmware `gpio=7` directive; compensates for the Pi 4's GPIO
   output-state persistence across reboots).
2. `hil-can-up.service` — brings `can0`, `can1`, `can2` up at
   500 kbps, sample point 0.6875, with `txqueuelen=1000` and
   `restart-ms=200` — and fails if the sample point didn't take.
3. `hil-broker.service` — starts the broker daemon; depends on both.
4. `hil-dashboard.service` — Flask UI on `:8080`; broker client.
5. `hil-bench-watchdog.timer` → `hil-bench-watchdog.service` — every
   5 min, verifies the bench and recovers it if it has wedged (see the
   [operator guide](operator-guide.md#self-healing-and-recovery)).
   **Enable the timer**: the service on its own never fires, which
   looks exactly like a bench that never wedges.

Install them:

```sh
pi$ cd ~/IFS_HIL/infra/systemd
pi$ sudo cp hil-psu-on.service hil-can-up.service \
            hil-broker.service hil-dashboard.service \
            hil-bench-watchdog.service hil-bench-watchdog.timer \
            /etc/systemd/system/
pi$ sudo systemctl daemon-reload
pi$ sudo systemctl enable hil-psu-on.service \
                          hil-can-up.service \
                          hil-broker.service \
                          hil-dashboard.service \
                          hil-bench-watchdog.timer
```

`hil-agent.service` also ships in that directory; leave it — it belongs
to a retired design and is deliberately not installed (see
[`infra/systemd/README.md`](../infra/systemd/README.md)).

Do **not** `systemctl start` them yet — they need the patched kernel
module and overlay active, which only happens after reboot.

---

## 8. Reboot and verify

```sh
pi$ sudo reboot
```

Wait about 30 seconds, reconnect, and run through this checklist:

```sh
pi$ # kernel driver bound to all three chips
pi$ sudo dmesg | grep mcp251x
# Expected:
#   mcp251x: loading out-of-tree module taints kernel.
#   mcp251x spi0.2 can0: MCP2515 successfully initialized.
#   mcp251x spi0.1 can1: MCP2515 successfully initialized.
#   mcp251x spi0.0 can2: MCP2515 successfully initialized.

pi$ # canN interfaces up at 500 kbps
pi$ ip -br link | grep can
# Expected: can0 UP, can1 UP, can2 UP

pi$ # the spidev nodes for the non-CAN chips — all nine of them
pi$ ls /dev/spidev0.{3..11}
# Expected: /dev/spidev0.3 … /dev/spidev0.11. Only spidev0.3 means the
# old overlay is installed (section 4) — the DACs and ADCs would fall
# back to userspace chip-selects.

pi$ # PSU_ON (GPIO7) driven LOW, PWR_OK (GPIO8) reads HIGH
pi$ pinctrl get 7,8
# Expected:   7: op -- pn | lo // GPIO7 = output
#             8: ip    pd | hi // GPIO8 = input
# NOTE: comma-separated. `pinctrl get 7 8` fails with "Too many arguments"
# on current pinctrl.

pi$ # broker and its socket up
pi$ systemctl status hil-broker --no-pager | head
pi$ ls -l /run/hil-broker/broker.sock
# Expected: socket present, broker "active (running)"

pi$ # dashboard service up and listening on 8080
pi$ systemctl status hil-dashboard --no-pager | head
pi$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/status
# Expected: dashboard "active (running)", HTTP 200

pi$ # watchdog timer scheduled
pi$ systemctl list-timers hil-bench-watchdog.timer --no-pager
# Expected: one row, with a NEXT time a few minutes out
```

Or check the whole build in one go — this runs every assertion in this
guide and names the section to redo for anything that fails:

```sh
pi$ cd ~/IFS_HIL && python3 -m tools.bench doctor
# Expected, on a correctly built bench:
#   ...
#   this bench matches the documented build
```

(`doctor` looks for `/dev/spidev0.3` only — keep the `ls` above for the
per-device nodes.)

If any check fails, go to
[`docs/troubleshooting.md`](troubleshooting.md) before proceeding.

---

## 9. Reach the dashboard

The dashboard is already running as `hil-dashboard.service` (enabled
in step 7, started on the reboot in step 8). Point a browser at
`http://<pi-ip>:8080/` — you should see PSU state, carrier power
monitors, ADC/DAC channels, CAN mode indicators, and TCA9555 I/O
state, all updating.

If port 8080 is unreachable:

```sh
pi$ systemctl status hil-dashboard      # is it running?
pi$ journalctl -u hil-dashboard -b -n 50 --no-pager
pi$ pkill -f 'dashboard/app.py'         # kill any stray nohup
pi$ sudo systemctl restart hil-dashboard
```

---

## 10. Install `can-flasher`

`can-flasher` is the host-side tool that speaks the STM32 bootloader
protocol. It's a Rust binary published as a release of `isc-fs/MingoCAN`
(the old `isc-fs/can-flasher` URL redirects there).

**Install 2.8.0 or newer, and upgrade an older one.** Builds before 2.8.0
have no ISO-TP session recovery ([MingoCAN#506], fixed by #527) and NACK
`BAD_SESSION` part-way through a large image — *after* the erase, so the
carrier is left with no app and cannot be written back by the same tool.
bench-01 sat on 2.5.5 and lost its AMS app exactly that way; 2.14.0 wrote
the same 151072 B image first try. `tools/flash_dut.py` refuses to start
on anything older, before it energises anything.

[MingoCAN#506]: https://github.com/isc-fs/MingoCAN/issues/506

On a workstation with `gh` authenticated:

```sh
# pick the current release; do not pin an old one -- v1.1.2 was pinned here
# for a long time while benches ran 2.5.5, and newer versions carry the
# `logs` subcommand the AMS LOGFS work depends on.
$ VER=$(gh release view -R isc-fs/MingoCAN --json tagName --jq .tagName)
$ gh release download "$VER" -R isc-fs/MingoCAN \
       -p "can-flasher-$VER-aarch64-unknown-linux-gnu.tar.gz"
$ scp can-flasher-$VER-aarch64-unknown-linux-gnu.tar.gz isc@<pi-ip>:/tmp/
```

On the Pi:

```sh
pi$ cd /tmp
pi$ tar -xzf can-flasher-*-aarch64-unknown-linux-gnu.tar.gz
pi$ sudo install -m 0755 \
       can-flasher-*-aarch64-unknown-linux-gnu/can-flasher \
       /usr/local/bin/
pi$ can-flasher --version     # must be >= 2.8.0 (bench-01 runs 2.14.0)
pi$ can-flasher adapters      # expect: SocketCAN interfaces: can0 can1 can2
```

---

## 11. First discovery

**Important** gotcha: the kernel's `mcp251x` probes SPI children in
reverse order, so the kernel `canN` names are **inverted** relative
to the PCB labels:

| kernel netdev | PCB label | MLC carrier ECUs live here |
|---|---|---|
| `can0` | CAN3 (U21) | — |
| `can1` | CAN2 (U19) | — |
| `can2` | **CAN1 (U17)** | ✅ yes |

The MLC1..MLC4 carriers are all wired to PCB CAN1, which is kernel
`can2`. Any flash command targets `can2`.

Put your carrier (say MLC1) under power — via the dashboard's
"Carrier 1 power" toggle or from the shell:

```sh
pi$ export HIL_BROKER_SOCKET=/run/hil-broker/broker.sock
pi$ python3 -c "
from broker.server import BrokerClient
c = BrokerClient('${HIL_BROKER_SOCKET}')
c.call('tca.set_direction', addr=0x20, port=0, mask=0x00)
c.call('tca.write_pin', addr=0x20, port=0, pin=0, value=True)  # K1 on
print('MLC1 current:', c.call('ina.current', addr=0x40) * 1000, 'mA')
"
```

A running STM32 bootloader draws about **130 mA**. If you see ≤ 1 mA,
the carrier isn't powered — check the relay and fuse before
proceeding. See [`docs/troubleshooting.md`](troubleshooting.md).

Run discovery:

```sh
pi$ can-flasher discover -i socketcan -c can2 --timeout-ms 3000
```

Expected output:

```
Node  Proto  FW Version        Git Hash  Product  WRP  Reset Cause
────  ─────  ────────────────  ────────  ───────  ───  ───────────
0x01  0.1    no app installed  —         —        ✗    PIN
```

Node ids are provisioned into each carrier's bootloader: the scheme is
ECU `0x01`, AMS `0x02`, uDV `0x03`, and a fresh bootloader starts at
`0x01`. If two powered carriers answer on the same id you'll see
collisions in the ISO-TP reassembler output. Power one carrier at a time
for first runs, or provision distinct node IDs via `can-flasher config`.

---

## 12. First flash

Use any `.bin` for the STM32H733ZG. The
[`can-flasher`](https://github.com/isc-fs/MingoCAN) repo ships a
trivial demo at `demo/MAIN_IFS08_DEMO.bin` — copy it to the Pi:

```sh
$ scp /path/to/MAIN_IFS08_DEMO.bin isc@<pi-ip>:/tmp/
```

Flash and jump:

```sh
pi$ can-flasher \
      --interface socketcan --channel can2 --bitrate 500000 \
      --node-id 0x1 --timeout 10000 \
      flash /tmp/MAIN_IFS08_DEMO.bin \
      --address 0x08020000 --verify-after --jump
```

Expected tail:

```
Committing metadata…
Done — erased 1 written 1 skipped 0 in …
Flashed … crc=0x…, size=26172 B …
jumped to app at 0x08020000.
```

Running `can-flasher discover -i socketcan -c can2` after the jump
should return **no** bootloaders — the app has control and is not
listening on the BL CAN IDs. That's success.

For the real ECU and AMS images, use the wrapper once the bench has a
descriptor (section 14): `python3 -m tools.flash_dut --dut ecu --bin
<ECU08.bin>` powers only that carrier, gates on the bootloader's
identity, and is what CI runs — see the
[operator guide](operator-guide.md#flashing-an-ecu).

---

## 13. Verification checklist

At this point you have:

- [x] Kernel `mcp251x` driver binding all three MCP2515s.
- [x] `can0`/`can1`/`can2` up at 500 kbps, `txqueuelen=1000`.
- [x] `/dev/spidev0.3`–`0.11` present — every chip-select kernel-owned.
- [x] `hil-broker` running as a systemd service with the socket at
      `/run/hil-broker/broker.sock`.
- [x] Dashboard serving at `http://<pi-ip>:8080/`.
- [x] `hil-bench-watchdog.timer` scheduled.
- [x] `can-flasher` ≥ 2.8.0 installed and able to discover + flash an ECU.

You're done. Anything else — regression tests, multi-ECU flashing,
CI wiring — is the operator guide's territory.

---

## 14. Join the fleet (self-hosted runner)

Required for dispatched runs (`.github/workflows/hil-test.yml`). Without a
runner the test job sits in **Queued** forever rather than failing — GitHub
does not error on an unmatched `runs-on` — so the workflow checks for one up
front and tells you if it is missing.

First describe the bench, then register a runner carrying exactly the labels
the descriptor declares:

```sh
# on your laptop, from the repo root
python -m tools.bench describe --draft bench-02 --out configs/benches/bench-02.yaml
# fill in the FIXMEs, then:
python -m tools.bench validate
python -m tools.bench labels --bench bench-02
#   -> self-hosted,hil-bench,bench-02,dut-ams,stim-cells,...
```

```sh
# on the bench
mkdir -p ~/actions-runner && cd ~/actions-runner
# The asset name embeds the version, so .../latest/download/<name> 404s.
RUNNER_VER=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p')
curl -fsSL -o runner.tar.gz \
  "https://github.com/actions/runner/releases/download/v${RUNNER_VER}/actions-runner-linux-arm64-${RUNNER_VER}.tar.gz"
tar xzf runner.tar.gz

# a registration token is short-lived; mint one with:
#   gh api -X POST repos/isc-fs/IFS_HIL/actions/runners/registration-token --jq .token
./config.sh --url https://github.com/isc-fs/IFS_HIL \
            --token <REGISTRATION_TOKEN> \
            --name bench-02 \
            --labels "$(python -m tools.bench labels --bench bench-02)"

sudo ./svc.sh install "$(id -un)"   # svc.sh only exists after config.sh
sudo ./svc.sh start
```

Then give the runner service a restart policy. The unit `svc.sh`
generates has none, so a runner that *exits* never comes back — and
they do exit: on 2026-09-18 GitHub told bench-01's runner its
registration had been deleted (it hadn't), the runner quit cleanly, and
every dispatched run queued against it for a week.

```sh
U=$(systemctl list-unit-files 'actions.runner.*.service' --no-legend | awk '{print $1}')
sudo mkdir -p /etc/systemd/system/$U.d
sudo cp ~/IFS_HIL/infra/systemd/actions.runner.restart.conf /etc/systemd/system/$U.d/restart.conf
sudo systemctl daemon-reload
sudo systemctl enable "$U"                  # comes back after a power cut
systemctl show -p Restart --value "$U"      # expect: always
```

`bench_setup.sh` does all of this in its runner phase, `bench doctor`
checks both the enablement and the effective restart policy, and the
watchdog logs `RUNNER DOWN` if the runner is ever not active.

The labels **are** the routing table: a dispatch asks for capabilities, the
resolve job turns those into labels, and GitHub picks a bench that carries
them. If you rewire a bench, update its descriptor *and* re-run `config.sh`
with the new label set, or it will keep attracting runs it can no longer serve.

> A self-hosted runner executes workflow code from this repository on the bench
> host. Keep the repo's write access to people you would trust with the
> hardware.


---

## Where to go next

- **Taking the project over** — [`HANDOVER.md`](../HANDOVER.md).
- **Operating the bench day-to-day** —
  [`docs/operator-guide.md`](operator-guide.md).
- **Something broke** —
  [`docs/troubleshooting.md`](troubleshooting.md).
- **Understanding what the broker exposes** —
  [`docs/broker-api.md`](broker-api.md).
- **Understanding why the driver had to be patched** —
  [`docs/design/mcp251x-driver-patches.md`](design/mcp251x-driver-patches.md).
