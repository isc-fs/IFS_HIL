# Quickstart — a new bench, in about an hour

The short path. [`getting-started.md`](getting-started.md) is the same journey
with the reasoning attached; read that when something surprises you, or before
changing any of it.

**Realistic budget: 60–90 minutes of hands-on time**, most of it waiting — an
apt install, a kernel module compiling on the Pi, and one reboot. The typing is
about ten minutes.

---

## Before you start

None of this is quick if a prerequisite is missing, so check first:

| | |
|---|---|
| **Raspberry Pi 4** (2 GB+), Pi OS Lite **64-bit** Bookworm+, SSH on | |
| **BACKPLANE_HIL PCB**, populated | see the design review |
| **ATX PSU with ≥ 3 A on +5 V standby** | less causes Pi undervoltage that breaks SPI |
| **A carrier with the bootloader already burned** and its node id provisioned (ECU `0x01`, AMS `0x02`) | via SWD, out of band — the bench cannot do this |
| **`gh` authenticated** on your workstation | `can-flasher` is a private release |
| **Passwordless sudo** on the Pi | the bootstrap installs packages, a kernel module and units |

---

## One script, run it until it stops asking

```sh
pi$ git clone https://github.com/isc-fs/IFS_HIL.git && cd IFS_HIL
pi$ ./scripts/bench_setup.sh --bench bench-NN --dry-run   # read what it will do
pi$ ./scripts/bench_setup.sh --bench bench-NN
```

`bench_setup.sh` is **resumable**. Setup spans a reboot and one editing task
that no script can do for you, so it runs as far as it can, tells you what it
needs, and continues from there when you re-run it with the same arguments.
It is idempotent throughout — re-running is always safe, and on a finished
bench it changes nothing and says so.

| Phase | What happens | You do |
|---|---|---|
| **base** | interfaces, groups, packages, Python package, overlay + boot config, patched `mcp251x`, sudoers, systemd units + the watchdog timer | nothing — ~30 min, mostly waiting |
| **reboot** | stops and asks | `sudo reboot`, then re-run |
| **host** | `bench doctor` — every assertion in the long guide | fix anything red before continuing |
| **flasher** | installs `can-flasher` (≥ 2.8.0, upgrading an older one) if `gh` is authenticated here, otherwise tells you to do it from a workstation | maybe §10 |
| **descriptor** | drafts `configs/benches/bench-NN.yaml` from a live probe, then stops | **fill the FIXMEs** (below), re-run to validate + verify |
| **runner** | registers a self-hosted runner with this bench's labels | supply `--runner-token`, or let it mint one via `gh`; then re-run once more — that pass adds the runner's `Restart=always` drop-in |

### The descriptor is the part only you can do

`describe` fills in what it can probe — I²C addresses, CAN devices, slot map.
You supply what no probe can know: which DUT is in which slot, what the
fixtures are wired to, who owns the bench.

**Declare a capability only if the bench really has it.** These labels route
other people's test runs; an optimistic one silently attracts work this bench
cannot serve. That is why the draft leaves `capabilities` empty and validation
fails until you have thought about it.

Commit it and open a PR — the fleet inventory lives in the repo, so the
resolve job can route without contacting any bench.

### Then a dispatched run should land on it

```sh
$ gh workflow run hil-test.yml --ref dev -f bench=bench-NN -f suite=tests/hil/test_can.py
```

`--ref` matters: the repo's default branch is `main`, which does not carry
`hil-test.yml`. Use `dev` once your descriptor PR has merged, or your PR's
branch before then. A green check is not proof that anything ran — see
[troubleshooting](troubleshooting.md#firmware-pr-looks-green-but-no-hil-verdict-ever-arrives).

---

## What this does **not** give you

A bench built to here can power carriers, talk CAN, and flash firmware. It
**cannot** run the AMS suite, because the stimulus hardware is not covered by
the bootstrap or by the bringup guide:

- **Pico LTC emulator** — cell voltages and NTC temperatures
  (`stim-cells`, `stim-temps`). See
  [`pico_ltc_emulator.md`](pico_ltc_emulator.md), which is not yet part of the
  bringup path.
- **NTC interposer** — physically opening a sensor (`fault-temp-open`).
  Undocumented; ask whoever built bench-01.
- **Pack-current fixture** — DAC into the AMS current front-end
  (`stim-pack-current`). Wiring lives in bench-01's descriptor under
  `routing.pack_current`, not in any build guide.

Until those are documented, a second bench can honestly declare `dut-*` and
little else. That is a known gap, not an oversight on your part.

## Starting and stopping the bench

There is no launch script, and you do not need one: **systemd brings the whole
bench up at boot**, in dependency order — `hil-psu-on` → `hil-can-up` →
`hil-broker` → `hil-dashboard`. Power the Pi on and the bench is live, and
`hil-bench-watchdog.timer` then checks it every 5 minutes and recovers a wedge
on its own.

For manual control:

```sh
pi$ sudo systemctl start hil-psu-on hil-can-up hil-broker hil-dashboard hil-bench-watchdog.timer
pi$ sudo systemctl stop  hil-bench-watchdog.timer hil-dashboard hil-broker hil-can-up hil-psu-on
pi$ systemctl is-active  hil-psu-on hil-can-up hil-broker hil-dashboard hil-bench-watchdog.timer
pi$ python3 -m tools.bench doctor      # the four services, plus the host build
```

Include the timer when stopping: left running, the watchdog brings the bench
back within five minutes — it pulls `hil-broker` up, and with it the PSU and CAN
units.

`hil-psu-on`'s `ExecStop` drops `PS_ON#`, so stopping it powers the ATX rails
down. Stop it last, and expect every SPI peripheral to go dark when you do.

> **`scripts/launch.sh` is gone** (removed 2026-08-31). It belonged to the
> accu-charger project — it installed Docker and added a CAN overlay that
> conflicts with `mcp2515-triple`. An rsync'd bench copy can still carry it
> (`sync_to_pi.sh` never deletes); do not run it.

## Two gotchas worth knowing on day one

- **A DAC can wedge.** If a DAC80504 reports a device id other than `0x0417`
  (`0x0000`, `0x3FFF`), a broker restart will not fix it and neither will a
  reboot — it needs a real `PWR_OK` transition, with CAN and the broker rebuilt
  behind it: `python3 -m tools.bench recover --bench <id> --level 2`. The
  watchdog runs the same ladder by itself. `verify` surfaces it; `doctor` does
  not.
- **`pinctrl get 7 8` is wrong**; the accepted form is `pinctrl get 7,8`. Older
  copies of the guide had the space-separated version, which errors.
