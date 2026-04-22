# Architecture

A top-down tour of the HIL bench. Read this once when you join the
project; return for component responsibility questions later.

Pair with [`hardware-reference.md`](hardware-reference.md) for the
signal-level details and
[`design/broker-migration.md`](design/broker-migration.md) for the
"how did we get here" history.

---

## One-paragraph summary

The HIL bench is a Raspberry Pi 4 plugged into a custom backplane
PCB (BACKPLANE_HIL) that hosts three CAN controllers, four MLC
carrier slots (each with an STM32 ECU, an INA226 power monitor, and
a relay), three TCA9555 I/O expanders, four DAC80504s, three
MCP3208s, and an ATX PSU interface. Kernel `mcp251x` (patched
out-of-tree) drives the CAN chips and exposes them as SocketCAN
`canN` netdevs. A Python daemon — `hil-broker` — is the single
owner of SPI, I²C, and GPIO; it serialises hardware access across
the rest of the stack (a Flask dashboard, the pytest HIL suite, and
an RPC helper library that tests use via `from tools.hil_client
import …`). CAN flashing is done by the separate Rust
`can-flasher` binary, which talks to `canN` directly via SocketCAN.
A set of systemd units (`hil-psu-on`, `hil-can-up`, `hil-broker`)
brings the bench into a known state on every boot.

---

## Layered view

```
                        ┌─────────────────────────────────────┐
Clients (user-space) →  │ dashboard │ pytest HIL │ can-flasher │
                        └─────┬─────┴─────┬──────┴──────┬──────┘
                              │ Unix-RPC  │ same        │ AF_CAN
                              ▼           ▼             │
                        ┌───────────────────────┐       │
Broker                  │      hil-broker        │       │
(thread-safe            │  (Python, systemd)     │       │
 mediator)              │  per-bus locks         │       │
                        └─┬──────────┬──────────┘        │
                          │          │                   │
              /dev/spidev0.3   /dev/i2c-1      sockets    │
                          │          │                   │
                          ▼          ▼                   ▼
                    ┌─────────────────────────────────────────┐
Kernel              │  spi-bcm2835    i2c-bcm2835     mcp251x │
                    │                                 (patched)│
                    └─────┬───────────┬──────────────────┬─────┘
                          │           │                  │
                          ▼           ▼                  ▼
                    ┌─────────────────────────────────────────┐
Hardware            │                BACKPLANE_HIL PCB         │
                    │ MCP3208 ×3   INA226 ×4    MCP2515 ×3     │
                    │ DAC80504 ×4  TCA9555 ×3   + transceivers │
                    │ nRF24 (np)   Q5 + SN74LVC125A MISO buffer│
                    │                                          │
                    │ ATX rails:  +12V → relay coils            │
                    │             +5V SBY → LDO → +3V3 SBY →   │
                    │                              I²C devices  │
                    │             +3V3 (main) → SPI devices     │
                    │                                          │
                    │ MLC1–MLC4 carrier slots (STM32H733ZG)    │
                    └─────────────────────────────────────────┘
```

---

## Components and responsibilities

### Hardware layer — BACKPLANE_HIL PCB

Single-board backplane with:

- Three MCP2515 + SN65HVD230 CAN channels. PCB CAN1 is wired to
  the MLC carrier bus (where the ECUs under test live); PCB CAN2
  and CAN3 are available for future use (e.g. simulating a second
  vehicle subsystem).
- Four MLC carrier slots. Each carrier is a separate daughter
  board with an STM32H733ZG. The carrier connects to +12V via a
  relay coil (K1–K4) and to the MLC-bus CAN1 transceiver.
- Four INA226 current monitors, one per MLC slot, low-side
  sensing (so `bus_voltage()` reads ~0 V by design).
- Three TCA9555 I/O expanders on I²C. TCA0 (`0x20`) port 0 bits
  0–3 drive the Q1–Q4 NMOSFETs that energise the K1–K4 relay coils.
- Three MCP3208 8-channel ADCs and four DAC80504 4-channel DACs
  for analog stimulus / response, sharing SPI0 with the CAN
  controllers.
- SN74LVC125A tri-state buffer on the SPI data lines (MOSI, MISO,
  SCK); its `~OE` is gated by ATX `PWR_OK` through a small-signal
  NMOS (Q5). The SPI bus to every peripheral is therefore silent
  when the ATX PSU is off — a feature, not a bug.
- ATX PSU control: `PS_ON#` out on GPIO7, `PWR_OK` in on GPIO8.

Full signal map in [`hardware-reference.md`](hardware-reference.md).

### Kernel layer

- **`spi-bcm2835`** — stock Raspberry Pi SPI0 master driver.
- **`mcp251x`** — **patched** out-of-tree build in
  [`infra/kernel-module/mcp251x-patched/`](../infra/kernel-module/mcp251x-patched/).
  The stock driver can't probe on this board because of three
  hardware quirks around single-burst SPI reads, the RESET
  instruction, and a CANCTRL read-back register. Five targeted
  patches fix those; see
  [`design/mcp251x-driver-patches.md`](design/mcp251x-driver-patches.md).
- **`i2c-bcm2835`** — stock I²C driver for `/dev/i2c-1`.
- **`can-dev` + `can-raw`** — SocketCAN kernel layer. The kernel
  brings up `can0` / `can1` / `can2` from the three `mcp251x`
  devices.
- **Device-tree overlay** at
  [`infra/devicetree/mcp2515-triple.dts`](../infra/devicetree/mcp2515-triple.dts)
  tells the kernel to bind all three MCP2515s, wires the
  interrupts (GPIO4/5/6), configures SPI mode 3, the 16 MHz
  fixed-clock node, and leaves GPIO7/8 alone for our PSU control.
  Also adds a spare `spidev@3` on the unused GPIO16 so the
  register-level Python driver retains a userspace spidev for the
  non-CAN chips.

### System services

Three systemd units, all in [`infra/systemd/`](../infra/systemd/),
in dependency order:

1. **`hil-psu-on.service`** — oneshot at `sysinit.target`,
   re-asserts `PS_ON#` (GPIO7) low and forces GPIO8 back to
   input+pull-down. Compensates for the Pi 4's GPIO output-state
   persistence across reboots (a prior userspace toggle can defeat
   the firmware `gpio=7=op,dl` directive).
2. **`hil-can-up.service`** — oneshot; runs `ip link set canN up
   type can bitrate 500000 restart-ms 200` and `txqueuelen 1000`
   on all three interfaces. The `txqueuelen=1000` is required to
   sustain a full-speed flash write (default 10 overflows).
3. **`hil-broker.service`** — `simple` long-running; exec-starts
   `python3 -m broker.server --socket /run/hil-broker/broker.sock`
   as the `isc` user, with the sudoers drop-in at
   [`infra/sudoers.d/hil-broker`](../infra/sudoers.d/hil-broker)
   granting narrow `ip link set canN …` escalation.

### `hil-broker` — the mediator

The broker is the single owner of SPI/I²C/GPIO on the Pi. Its job
is to serialise every hardware access across clients so
the dashboard, the test suite, and any ad-hoc scripts don't
corrupt each other's transactions.

Structure:

```
broker/
├── server.py    — Unix-socket listener, per-connection handler,
│                  also exposes BrokerClient (the Python client
│                  class that clients import).
├── rpc.py       — Transport-agnostic JSON-RPC dispatcher.
│                  Method table maps "dac.set_voltage" → backend
│                  method; handles request framing, errors, IDs.
├── bus.py       — HardwareManager: holds the real SPI / I²C /
│                  GPIO handles and driver instances. Per-bus
│                  locks make concurrent RPC calls safe.
└── fake_bus.py  — In-memory backend implementing the same
                   Protocol. Used by off-bench unit tests and
                   laptop development.
```

Concurrency:

- One `threading.Lock` each for the SPI bus, the I²C bus, the
  GPIO subsystem, and the CAN link-state changes.
- Each incoming RPC acquires the lock for the underlying bus and
  releases it when the driver call returns. Operations on
  **different** buses (e.g. SPI DAC write and I²C INA226 read)
  run in parallel. Operations on the **same** bus serialise.
- The broker is multi-threaded (one thread per connected client),
  but there are at most three concurrent clients in practice
  (dashboard, optionally a test run, optionally an ad-hoc shell).

Full method table in [`broker-api.md`](broker-api.md).

### Clients

Three consumers talk to the broker. None of them should open
`/dev/spidev*`, `/dev/i2c-*`, or `/dev/gpiochip*` directly —
that's the whole point of the mediator.

**Dashboard** — [`dashboard/app.py`](../dashboard/app.py)

- Flask web server on port 8080.
- One background thread polls the broker every 2 s for all
  sensors, caches the result in memory, serves `/api/status` from
  the cache (so page loads never wait on hardware).
- Control endpoints (`/api/psu/power`, `/api/dac/…`, etc.) are
  RPC passthroughs with input validation.
- See [`dashboard.md`](dashboard.md) for the HTTP API.

**HIL pytest suite** — [`tests/hil/`](../tests/hil/)

- Fixtures construct client proxies from
  `tools.hil_client.MCP3208(idx=N)` etc. Each proxy call translates
  to a broker RPC.
- Tests auto-skip if the broker socket isn't reachable, so an
  off-bench `pytest tests/` doesn't error out.
- The unit-test counterpart at [`tests/broker/`](../tests/broker/)
  exercises the dispatcher and fake backend without touching any
  real hardware.

**`can-flasher`** — the Rust binary from
[isc-fs/can-flasher](https://github.com/isc-fs/can-flasher)

- Does **not** go through the broker. Binds directly to the
  SocketCAN netdev `can2`.
- Rationale: the broker's job is to serialise SPI and I²C, which
  the flasher doesn't touch; SocketCAN is already multi-client
  through the kernel. Keeping the flasher transport-independent
  also means it's trivially usable with other adapters (CANable
  via SLCAN, PCAN on Windows, etc.) with no broker in the loop.

### CI/CD loop

```
  ┌───────────────┐                    ┌──────────────────┐
  │  External     │  /hil-build PR     │  GitHub Actions  │
  │  firmware repo│ ───── comment ───▶ │  hil-build-      │
  │               │                    │  trigger.yml     │
  └───────────────┘                    └────────┬─────────┘
                                                │
                                                ▼
                                       ┌──────────────────┐
                                       │  hil-build-only  │
                                       │  .yml (Docker +  │
                                       │  arm-gcc)        │
                                       └────────┬─────────┘
                                                │ artifact (.bin)
                                                ▼
                                       ┌──────────────────┐
                                       │  hil-flash.yml   │
                                       │  (self-hosted    │
                                       │  runner on Pi)   │
                                       └────────┬─────────┘
                                                │
                                                ▼
                                       ┌──────────────────┐
                                       │  can-flasher     │
                                       │  flash on canN   │
                                       └────────┬─────────┘
                                                │
                                                ▼
                                       🟢 / 🔴 comment on PR
```

The three workflows live under
[`.github/workflows/`](../.github/workflows/). They're stubbed
today: trigger and build pieces are wired; the flash-on-runner
path is the next big integration step.

---

## Important invariants

A few things the whole system relies on. If any of these breaks,
expect weird failures:

1. **The broker is the only `/dev/spidev0.3`, `/dev/i2c-1`,
   `/dev/gpio*` opener.** If something else holds one of these open,
   you will see intermittent bus corruption under load.
2. **The kernel `mcp251x` driver owns `spi0.0`, `spi0.1`, `spi0.2`.**
   Do not try to open `/dev/spidev0.0/1/2`; they don't exist when
   the overlay is loaded, and trying will wedge things if the
   overlay is ever removed.
3. **CAN netdev ↔ PCB label is inverted** (`can0` = CAN3,
   `can2` = CAN1). Don't try to "fix" this in software — it's a
   property of the kernel's probe order on this board and changing
   the overlay's `reg` numbers would just swap the kernel name
   mapping, breaking every doc and script that already assumes
   `can2 = CAN1`.
4. **Firmware `gpio=7=op,dl` must be in `/boot/firmware/config.txt`.**
   Without it, the kernel probes `mcp251x` before the PSU is on,
   and the probe fails. `hil-psu-on.service` is a belt-and-braces
   re-assertion, not a substitute.
5. **`txqueuelen` on `canN` must be > 10**, or sustained flashes
   hit ENOBUFS. `hil-can-up.service` sets 1000.

---

## Related documents

- [`hardware-reference.md`](hardware-reference.md) — signal map.
- [`broker-api.md`](broker-api.md) — every RPC method.
- [`dashboard.md`](dashboard.md) — HTTP API.
- [`design/broker-migration.md`](design/broker-migration.md) —
  the phased plan that got us here.
- [`design/mcp251x-driver-patches.md`](design/mcp251x-driver-patches.md) —
  why the kernel module is patched and what each patch does.
- [`design/phase-history.md`](design/phase-history.md) — timeline
  of migrations with PR links.
