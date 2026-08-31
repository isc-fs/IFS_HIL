# Phase history

A chronological record of the broker migration and the related
Phase-4 side quest that ended up dominating the effort. Keeps the
"what happened when" story in one place so future contributors can
follow the PR trail without reconstructing it from scratch.

If you want the design intent, read
[`broker-migration.md`](broker-migration.md). This document is just
the timeline.

---

## Phase 0 — Seam and migration plan

**Dates**: 2026-04-14 → 2026-04-15.

Laid the foundation for the broker migration without changing
runtime behaviour.

- Added `tools/hil_client.py` as a thin re-export of the existing
  driver classes. Callers who switched to `from tools.hil_client
  import DAC80504, …` were not yet going through a broker — they
  still got the direct-access classes — but they got the seam that
  Phase 2 would flip.
- Committed `docs/broker_migration_plan.md` describing the intent,
  the phased plan, IPC choice (Unix socket + line-delimited
  JSON-RPC), RPC surface v1, and risks.

Landed directly on `dev`.

Before this, the bench was the Phase-0 state captured in
**[PR #12](https://github.com/isc-fs/IFS_HIL/pull/12)** and
**[PR #13](https://github.com/isc-fs/IFS_HIL/pull/13)**: the
Flask dashboard, register-level Python drivers, pytest suite, with
the known contention caveat ("never run scripts while the dashboard
is up").

---

## Phase 1 — Broker skeleton

**PR**: [#14](https://github.com/isc-fs/IFS_HIL/pull/14) (2026-04-21).

Shipped:

- `broker/` package: `server.py` (Unix-socket threaded listener +
  `BrokerClient`), `rpc.py` (transport-agnostic dispatcher),
  `bus.py` (real backend with per-bus locks), `fake_bus.py`
  (in-memory backend that satisfies the same Protocol).
- RPC surface v1: 12 methods covering ADC, DAC, CAN, INA, TCA,
  PSU, health.
- `tests/broker/` — 14 unit + integration tests, all passing off
  the bench via the fake backend.
- `infra/systemd/hil-broker.service` — systemd unit file; not
  installed on the bench yet.
- `tools/__init__.py` wrapped in try/except so driver re-exports
  can fail benignly on non-RPi hosts (unblocks CI).

No client migrated. All existing callers continued using direct
driver imports.

Verified on-bench: broker started, accepted RPC, HIL suite still
passed.

---

## Phase 2 — Dashboard cutover

**PR**: [#15](https://github.com/isc-fs/IFS_HIL/pull/15) (2026-04-21).

- Extended broker RPC surface from 12 methods to 25, with
  per-driver methods to match `tools/*.py` 1:1 so proxy classes
  could be thin passthroughs (instead of repeatedly calling a
  "snapshot" wrapper and slicing).
- Flipped `tools/hil_client.py` from re-exports to real proxy
  classes. Constructor signatures kept as before (positional
  `spi`/`cs` args still accepted but ignored) so callers needed
  only the import line change.
- Rewrote `dashboard/app.py`:
  - Deleted `_init_hardware()` — broker owns all devices now.
  - Deleted `_hw_lock` — broker serialises cross-process.
  - Routed PSU through `psu_power()`/`psu_status()` helpers
    instead of direct GPIO.

Verified on-bench with `/proc/<broker>/fd/` — broker held
`/dev/spidev0.0`, `/dev/i2c-1`, `/dev/gpiomem`; dashboard held
zero of those. HIL suite still 93 passed / 11 skipped.

---

## Phase 3 — Pytest cutover

**PR**: [#16](https://github.com/isc-fs/IFS_HIL/pull/16) (2026-04-21).

- Extended broker with more driver-method variants needed by
  tests (`dac.reset`, `dac.read_device_id`, `can.init`,
  `can.loopback_test`, `can.int_level`, `ina.read_manufacturer_id`,
  etc.) plus `i2c.scan`.
- `tests/hil/conftest.py` rewritten: fixtures now construct broker
  proxies via `tools.hil_client`. `broker_available` session
  fixture auto-skips everything if the socket isn't reachable
  (so off-bench `pytest tests/` is clean).
- `tests/hil/test_can.py` rewritten to use `ctrl.int_level()`
  through the broker instead of direct `RPi.GPIO.input`.
- Deleted `tools/mcp2515.py` from the import chain; kept the file
  for historical reference.

**Milestone**: the "don't run tests while the dashboard is up"
rule went away. Verified on-bench: pytest + dashboard concurrent;
93 passed / 11 skipped; dashboard still returning HTTP 200
throughout.

---

## Phase 4 — CAN flasher integration (scope pivot)

This is the phase the original plan got wrong, as described in
[`broker-migration.md`](broker-migration.md#what-actually-shipped).
Original intent was to "wrap the flasher in broker RPC"; actual
delivery was "bring the CAN chips under the kernel `mcp251x`
driver so the flasher's SocketCAN path works natively."

### Phase 4 groundwork — patched module + overlay

**PR**: [#17](https://github.com/isc-fs/IFS_HIL/pull/17) (2026-04-21).

- `infra/devicetree/mcp2515-triple.dts` — custom device-tree
  overlay binding all three MCP2515s on cs-gpios GPIO27/17/18
  with IRQs on GPIO4/5/6, 16 MHz fixed-clock reference, SPI mode
  3, `pinctrl-0` restricting SPI0 to data pins so GPIO7/8 stay
  free for PSU signalling. Includes a `spidev@3` child on
  GPIO16 for the register-level Python driver's continued access
  to DACs/ADCs/NRF.
- `infra/kernel-module/mcp251x-patched/` — out-of-tree build of
  `mcp251x` with four patches (writeup:
  [`mcp251x-driver-patches.md`](mcp251x-driver-patches.md)):
  split-read, CANCTRL bootstrap, skip CANCTRL sanity check,
  always-split reads.
- `docs/phase4_mcp251x_blocker.md` (since moved into
  `mcp251x-driver-patches.md`) — full diagnostic trail.

End of PR #17: `can0/can1/can2` netdevs appear at boot; loopback
TX/RX round-trips cleanly on all three.

### Phase 4 fix — wake-from-sleep

**PR**: [#18](https://github.com/isc-fs/IFS_HIL/pull/18) (2026-04-21).

Under PR #17 alone, the `ip link set canN up` path (which calls
`mcp251x_hw_wake`) failed with `RTNETLINK Connection timed out`.
Root cause: on this board the chip doesn't wake reliably via
WAKIE. Added the fifth patch — `mcp251x_hw_wake` delegates to
`mcp251x_hw_reset`, which uses the RESET instruction to restart
the oscillator.

End of PR #18: `canN` reliably come up under any ordering.

### Phase 4 broker migration — SocketCAN backend

**PR**: [#19](https://github.com/isc-fs/IFS_HIL/pull/19) (2026-04-21).

- `tools/hw_config.py`: `SPI_DEVICE 0 → 3` (the `spidev@3` added
  by the overlay).
- `infra/devicetree/mcp2515-triple.dts`: extended cs-gpios to 4
  entries + `spidev@3` child.
- `infra/sudoers.d/hil-broker` — narrow `ip link set canN *`
  escalation so the broker can manage link state as `isc`.
- Broker CAN backend rewritten on top of `python-can` (socketcan
  backend) + `ip -s -d -j link show` for error-counter scraping.
  RPC surface preserved — dashboard and tests unchanged.
- `can.init(bitrate)` semantics changed: leave interface DOWN,
  caller selects NORMAL or LOOPBACK via `can.set_mode`. Rationale:
  a chip in NORMAL with no peer goes BUS-OFF immediately.
- `can.int_level` repurposed as a link-health proxy (1 = UP,
  0 = DOWN) since the physical INT GPIO is now kernel-owned.
- `tests/hil/test_can.py::test_int_pin_idle_high` renamed and
  rewritten as `test_link_up_in_loopback`.

Verified: broker unit tests 15/15, HIL suite 93 passed / 11
skipped, dashboard HTTP 200 throughout, every non-CAN RPC
(DAC / ADC / INA / TCA / NRF / PSU) verified through `spidev0.3`.

### Phase 4 wiring — systemd + sudoers + flasher install

**PR**: [#20](https://github.com/isc-fs/IFS_HIL/pull/20) (2026-04-22).

Closed the Phase 4 loop with declarative boot-time bring-up:

- `infra/systemd/hil-psu-on.service` — re-asserts `PS_ON#` in
  userspace at `sysinit.target`; compensates for the Pi 4's
  GPIO output-state persistence across reboots that can defeat
  the firmware `gpio=7=op,dl` directive.
- `infra/systemd/hil-can-up.service` — brings `canN` up at
  500 kbps with `txqueuelen=1000` and `restart-ms=200` before
  the broker starts. `txqueuelen=1000` is required for sustained
  flash writes (default 10 returns ENOBUFS mid-sector).
- `infra/systemd/hil-broker.service` updated to depend on both.
- `infra/systemd/README.md` documents install + the canN ↔ PCB
  mapping table.

### Side quest — `can-flasher` aarch64-linux binary

**Not a PR in this repo**; landed in
[isc-fs/can-flasher](https://github.com/isc-fs/can-flasher) as:

- Repo `isc-fs/can-flasher` commit `794509c` — added
  `aarch64-unknown-linux-gnu` target to the release CI matrix.
- Tag `v1.1.2` cut as a CI-only release (`Cargo.toml` 1.1.1 →
  1.1.2) to publish the new binary.

With the aarch64 binary published, installing the flasher on the
Pi is a plain download + extract instead of a `cargo install`
build from source.

### Phase 4 acceptance — end-to-end flash

Validated on-bench at the end of 2026-04-22:

- `can-flasher adapters` lists `can0`, `can1`, `can2`.
- `can-flasher discover -i socketcan -c can2 --timeout-ms 3000`
  shows bootloader node `0x01` on MLC1.
- `can-flasher … flash MAIN_IFS08_DEMO.bin --address 0x08020000
  --verify-after --jump` — erase, write, verify, jump.
- Post-flash `discover` is silent (app running, BL gone).
- `can-flasher … send-raw 0x001 03 06 01` — app ACKs on `0x011`,
  `NVIC_SystemReset`s, next `discover` sees BL back with
  `Reset Cause: SOFTWARE`.

Full Phase 4 chain proven.

---

## Phase 5 — Observability (future)

Not yet started. Scope outline in
[`broker-migration.md`](broker-migration.md#where-the-plan-still-holds).
Three tiers:

1. Per-RPC metrics + dashboard broker panel + flash audit log.
2. INA current-over-time chart, staleness indicators,
   `hil-dashboard.service`.
3. Prometheus scrape / Grafana (only if there's demand).

No PR yet.

---

## External CI work (pre-Phase-0)

For completeness, the Phase 0–5 history is built on an earlier
round of CI / Docker-build infrastructure that landed in the
2026-03 range:

| PR | Scope |
|---|---|
| [#1](https://github.com/isc-fs/IFS_HIL/pull/1), [#2](https://github.com/isc-fs/IFS_HIL/pull/2), [#3](https://github.com/isc-fs/IFS_HIL/pull/3) | Workflow bootstrap: optional token, workflow refactor, firmware-repo CMake toolchain. |
| [#4](https://github.com/isc-fs/IFS_HIL/pull/4), [#5](https://github.com/isc-fs/IFS_HIL/pull/5), [#7](https://github.com/isc-fs/IFS_HIL/pull/7) | Artifact packaging (bin/hex from elf; no `/` in names). |
| [#6](https://github.com/isc-fs/IFS_HIL/pull/6) | First failing DV STM32 test committed. |
| [#8](https://github.com/isc-fs/IFS_HIL/pull/8), [#9](https://github.com/isc-fs/IFS_HIL/pull/9), [#10](https://github.com/isc-fs/IFS_HIL/pull/10), [#11](https://github.com/isc-fs/IFS_HIL/pull/11) | PR-comment trigger bot + artifact-download link. |

These are the pieces that will hook into Phase 4's flasher to
close the loop on "firmware PR → CI flash → 🟢 / 🔴 comment."

---

## Read these next

- [`broker-migration.md`](broker-migration.md) — the design intent.
- [`mcp251x-driver-patches.md`](mcp251x-driver-patches.md) — the
  kernel patches in detail.
- [`../architecture.md`](../architecture.md) — current state.
