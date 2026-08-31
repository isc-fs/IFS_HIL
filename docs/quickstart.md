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
| **A carrier with the bootloader already burned** | via SWD, out of band — the bench cannot do this |
| **`gh` authenticated** on your workstation | `can-flasher` is a private release |
| **Passwordless sudo** on the Pi | the bootstrap installs packages, a kernel module and units |

---

## 1. Bootstrap (~30 min, mostly unattended)

```sh
pi$ git clone https://github.com/isc-fs/IFS08_HIL.git && cd IFS08_HIL
pi$ ./scripts/bench_bootstrap.sh --dry-run    # read what it will do
pi$ ./scripts/bench_bootstrap.sh
pi$ sudo reboot
```

That covers sections 1–7 of the long guide: interfaces and groups, packages,
the Python package with its `[bench]` extra, the device-tree overlay and boot
config, the patched `mcp251x` module, the sudoers drop-in, and the four systemd
units. It is idempotent — re-run it any time; it reports what is already done
and changes nothing else.

## 2. Check the build (~1 min)

```sh
pi$ cd ~/IFS08_HIL && python3 -m tools.bench doctor
```

Every check should pass, ending in `this bench matches the documented build`.
A failure names the section of [`getting-started.md`](getting-started.md) to
redo. Do not carry on past a red check — everything below assumes the host is
sound.

## 3. Install `can-flasher` (~5 min)

Not in the bootstrap: it is a private release, so it needs your `gh` auth.
[`getting-started.md` §10](getting-started.md).

## 4. Describe the bench (~10 min, the part only you can do)

```sh
$ python -m tools.bench describe --draft bench-NN --out configs/benches/bench-NN.yaml
$ python -m tools.bench validate      # tells you exactly what is still FIXME
```

`describe` probes the live bench and fills in what it can find — I²C addresses,
CAN devices, slot map. You supply what no probe can know: which DUT is in which
slot, what the fixtures are wired to, and who owns the bench.

**Declare a capability only if the bench really has it.** These labels route
other people's test runs; an optimistic one silently attracts work this bench
cannot serve. `capabilities` is deliberately empty in the draft so validation
fails until you have thought about it.

Then prove the description is honest, and open a PR for it:

```sh
pi$ python3 -m tools.bench verify --bench bench-NN
```

## 5. Join the fleet (~10 min)

Register a self-hosted runner carrying exactly the labels the descriptor
declares — [`getting-started.md` §14](getting-started.md). The labels *are* the
routing table. After that:

```sh
$ gh workflow run hil-test.yml -f capabilities=dut-ams -f suite=tests/hil/ams/...
```

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
`hil-broker` → `hil-dashboard`. Power the Pi on and the bench is live.

For manual control:

```sh
pi$ sudo systemctl start hil-psu-on hil-can-up hil-broker hil-dashboard
pi$ sudo systemctl stop  hil-dashboard hil-broker hil-can-up hil-psu-on   # reverse order
pi$ systemctl is-active  hil-psu-on hil-can-up hil-broker hil-dashboard
pi$ python3 -m tools.bench doctor      # everything above, plus the host build
```

`hil-psu-on`'s `ExecStop` drops `PS_ON#`, so stopping it powers the ATX rails
down. Stop it last, and expect every SPI peripheral to go dark when you do.

> **Ignore `scripts/launch.sh`.** It belongs to the accu-charger project, not
> this one — it installs Docker and would add a CAN overlay that conflicts with
> `mcp2515-triple`. It now refuses to run on a bench, but do not go looking for
> it as the way to start things.

## Two gotchas worth knowing on day one

- **The DAC bank latches.** If all four DACs report device id `0x0000` instead
  of `0x0417`, a broker restart will not fix it and neither will a reboot — only
  `psu.power(False)` then `(True)`, a real `PWR_OK` transition. `doctor` and
  `verify` both surface it.
- **`pinctrl get 7 8` is wrong**; the accepted form is `pinctrl get 7,8`. Older
  copies of the guide had the space-separated version, which errors.
