# IFS_HIL — Hardware-in-the-Loop testbench

Automated STM32 ECU firmware validation on a Raspberry Pi. The bench
orchestrates reproducible cloud firmware builds, CAN-based flashing,
hardware simulation (DACs, ADCs, relays, power monitoring), and
pytest-driven regression. Designed for the Formula Student ECU suite
(VCU, AMS, MicroDV, Inverter) and wired through the BACKPLANE_HIL PCB.

**Taking the project over?** Start with [`HANDOVER.md`](HANDOVER.md).

---

## What the bench does

1. A firmware PR (`isc-fs/IFS08-CE-ECU`, `isc-fs/IFS08-CE-AMS`) is
   labelled `hil-test`, or gets a `/hil-test [suite]` comment.
2. CI picks a bench that has the capabilities the run needs, checks the
   firmware out at the PR's exact commit, builds it from a recipe
   reviewed in this repo, and uploads the `.bin` artifact.
3. The bench flashes the target ECU over CAN via the
   [`can-flasher`](https://github.com/isc-fs/MingoCAN) bootloader
   protocol.
4. Runs the HIL pytest suite against the real hardware — injecting
   stimulus with DAC outputs, reading responses with ADCs and INA226
   power monitors, manipulating I/O via TCA9555 expanders and relays.
5. Posts the verdict — with a table of any failing cases — back to the
   originating firmware PR.

The bench itself is managed through a single
[`hil-broker`](broker/) process on the Pi — a mediator that owns SPI,
I²C, and GPIO, and exposes an RPC surface consumed by the dashboard,
the pytest fixtures, and the CI flash wrapper (`tools/flash_dut.py`).
Clients do not touch `/dev/*` directly.

---

## Architecture at a glance

```mermaid
flowchart TD
    CI["CI (GitHub Actions)<br/>builds firmware .bin<br/>at the PR's commit"]

    subgraph Pi["Raspberry Pi · BACKPLANE_HIL"]
        direction TB

        DASH["Dashboard<br/>(Flask)"]
        PYTEST["pytest HIL suite"]
        FLASH["can-flasher<br/>(Rust binary)"]

        BROKER["hil-broker (Python)<br/>serialises SPI / I²C / GPIO<br/>across every client"]

        SPI["/dev/spidev0.4–0.11<br/>DAC×4 · ADC×3 · nRF24"]
        I2C["/dev/i2c-1<br/>INA226×4 · TCA9555×3"]
        GPIO["/dev/gpio*<br/>PSU_ON · PWR_OK"]
        MCP["mcp251x (kernel)<br/>socketcan canN<br/>3× MCP2515"]

        DASH -- "Unix-socket RPC" --> BROKER
        PYTEST -- "Unix-socket RPC" --> BROKER
        FLASH -- "AF_CAN" --> MCP
        BROKER --> SPI
        BROKER --> I2C
        BROKER --> GPIO
    end

    CI -- "artifact (.bin)" --> FLASH
    MCP --> ECU["STM32 ECU under test<br/>(bootloader or running app)"]
```

---

## Getting started

- **Taking the project over** — [`HANDOVER.md`](HANDOVER.md): current
  state, how CI works, what is fragile, and first tasks.
- **In a hurry** — [`docs/quickstart.md`](docs/quickstart.md): the short
  path.
- **Fresh bench setup** — run
  [`scripts/bench_setup.sh`](scripts/bench_setup.sh) (resumable; it stops
  for one reboot), or follow
  [`docs/getting-started.md`](docs/getting-started.md), the
  explain-every-step version: from blank Pi OS to flashing an ECU in
  roughly 45 minutes.
- **Day-to-day operation** — see
  [`docs/operator-guide.md`](docs/operator-guide.md)
  for recipes: start/stop services, run the HIL suite, flash an ECU,
  view CAN traffic, recover from bus-off and from a wedged bench.
- **Testing a firmware PR** — label it `hil-test` (or comment
  `/hil-test`) and CI does the rest. Named suites, so a developer picks
  what runs instead of getting the whole tree:
  [`docs/development/testing.md`](docs/development/testing.md#running-a-suite-from-a-firmware-pr).
- **Hardware signal map** —
  [`docs/hardware-reference.md`](docs/hardware-reference.md)
  documents GPIO/I²C/SPI assignments, the CAN netdev ↔ PCB label
  inversion, MLC carrier wiring, and the PSU gating path.

---

## Repository layout

```
.
├── README.md                    you are here
├── HANDOVER.md                  maintainer handover: state, risks, first tasks
├── CLAUDE.md                    operator cheat-sheet, invariants, branch policy
├── pyproject.toml               Python package metadata and deps
├── broker/                      hil-broker: SPI/I²C/GPIO mediator
│   ├── server.py                Unix-socket JSON-RPC listener
│   ├── bus.py                   HardwareManager (real backend)
│   ├── fake_bus.py              In-memory backend for off-bench tests
│   └── rpc.py                   Method-table dispatcher
├── dashboard/                   Flask web UI, polls broker every 2 s
│   ├── app.py                   HTTP endpoints + poll loop
│   └── index.html               Dark-themed web UI
├── tools/                       Register-level chip drivers + helpers
│   ├── hw_config.py             Pin/address single source of truth
│   ├── hil_client.py            Client-side proxies (talk to broker)
│   ├── bench.py                 Fleet CLI: descriptors, suites, doctor, recover
│   ├── flash_dut.py             Flash a carrier safely (what CI runs)
│   ├── mcp3208.py  dac80504.py  ina226.py  tca9555.py  nrf24l01.py
│   ├── mcp2515.py               Legacy register-level CAN driver
│   └── flash.py                 Legacy Python flasher (deprecated)
├── tests/
│   ├── broker/                  Unit tests (fake backend, off-bench)
│   └── hil/                     On-bench: self-tests + vcu/ and ams/ DUT suites
├── docs/                        This folder — you are reading it
├── infra/
│   ├── devicetree/              mcp2515-triple.dts overlay source
│   ├── kernel-module/
│   │   └── mcp251x-patched/     Out-of-tree mcp251x module + patches
│   ├── systemd/                 hil-* units, watchdog timer, runner drop-in
│   ├── sudoers.d/               ip-link privilege drop-in
│   └── udev/                    USB device stable-naming rules
├── docker/                      Firmware build image (legacy CI chain only)
├── configs/                     Bench descriptors, firmware recipes, suites
│                                (+ legacy per-ECU YAML configs)
├── scripts/                     bench_setup.sh (new bench, start here),
│                                sync_to_pi.sh, build_stm32_binaries.sh
└── .github/workflows/           CI: hil-test (+ hil-fw-build), host-tests,
                                 bench-inventory; hil-build-*/hil-flash = legacy
```

The `firmware/` directory is intentionally empty; firmware sources
live in per-ECU repos and are pulled into the bench at CI time.

---

## Documentation index

**Onboarding**
- [`HANDOVER.md`](HANDOVER.md) — start here if you are taking the project over
- [`docs/quickstart.md`](docs/quickstart.md) — the short path to a working bench
- [`docs/getting-started.md`](docs/getting-started.md) — zero-to-flashing on a fresh Pi
- [`docs/operator-guide.md`](docs/operator-guide.md) — day-to-day recipes
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — symptom → cause → fix

**Reference**
- [`docs/architecture.md`](docs/architecture.md) — component diagram, CI chain, capability routing
- [`docs/hardware-reference.md`](docs/hardware-reference.md) — PCB signals, GPIO / I²C / SPI / CAN mapping
- [`docs/broker-api.md`](docs/broker-api.md) — every broker RPC method
- [`docs/dashboard.md`](docs/dashboard.md) — HTTP endpoints and web UI
- [`docs/pico_ltc_emulator.md`](docs/pico_ltc_emulator.md) — the Pico that emulates the AMS's LTC6811 chain
- [`infra/systemd/README.md`](infra/systemd/README.md) — every systemd unit, and the runner drop-in

**Design history**
- [`docs/design/broker-migration.md`](docs/design/broker-migration.md) — why the broker exists, phase plan
- [`docs/design/mcp251x-driver-patches.md`](docs/design/mcp251x-driver-patches.md) — the five out-of-tree patches
- [`docs/design/phase-history.md`](docs/design/phase-history.md) — timeline with PR links

**Development**
- [`docs/development/setup.md`](docs/development/setup.md) — dev environment, branch policy
- [`docs/development/testing.md`](docs/development/testing.md) — broker tests, HIL tests, fake backend, suites from a PR
- [`docs/development/kernel-module.md`](docs/development/kernel-module.md) — iterating on mcp251x

---

## License and contribution

Internal project for the ISC Racing Team Formula Student electronics
sub-system. Not licensed for external reuse without the team's
consent. Contributions from team members: see
[`docs/development/setup.md`](docs/development/setup.md) for the branch
and commit conventions.
