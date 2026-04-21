# Hardware Broker Migration Plan

## Motivation

Today, every process that wants to talk to the bench hardware opens `/dev/spidev*`, `/dev/i2c-1`, and `/dev/gpiochip0` directly. The dashboard, the pytest suite, and (soon) the CAN flasher all want access at the same time. There is no arbitration, and the symptoms have already appeared: concurrent SPI traffic corrupts DAC80504 registers, so the operational rule today is "never run scripts while the dashboard is up."

That rule does not survive the upcoming milestones. The CI flash job and the dashboard poll loop will run concurrently by design. We need one owner of the buses.

## Goal

Introduce a single long-lived **hardware broker** process that exclusively owns the SPI, I2C, and GPIO devices, and expose it over a local IPC to all clients (dashboard, pytest, flasher, CI runners). No Docker on the Pi. No microservice mesh. One mediator, thin clients.

## Non-goals

- Containerizing the runtime on the RPi.
- Replacing systemd with any other supervisor.
- Changing the firmware build pipeline (Docker stays there — that boundary is correct).
- Rewriting driver internals in `tools/`. The broker wraps them; it does not replace them.

## Target Architecture

```
               ┌─────────────────────────────────────────┐
               │            hil-broker (systemd)          │
               │  owns: /dev/spidev0.{0,1}, /dev/i2c-1,   │
               │        /dev/gpiochip0                    │
               │  wraps: tools/mcp3208, dac80504,         │
               │         mcp2515, ina226, tca9555, ...    │
               │  serializes: one bus op at a time        │
               └───────────────┬─────────────────────────┘
                               │  Unix socket /run/hil-broker.sock
                               │  JSON-RPC (line-delimited)
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
   dashboard/app.py       pytest (tests/hil)       flasher
        │                      │                      │
   tools/hil_client.py (thin stub, same API as today's direct imports)
```

### Broker responsibilities

- Open hardware handles once at startup, from `hw_config.py`.
- Serialize every bus transaction (single asyncio or threaded queue; SPI + I2C can be independent queues).
- Expose a minimal RPC surface (see below).
- Publish a heartbeat / health endpoint so the dashboard can show broker status.
- Log every operation with timestamps for post-mortem debugging.

### Client responsibilities

- No direct `spidev`, `smbus`, or `gpiod` imports.
- Go through `tools/hil_client.py`, which mimics today's driver call shapes so existing code barely diffs.

## IPC Choice

**Unix domain socket + line-delimited JSON-RPC.**

Rationale:
- Local-only, zero auth concerns.
- No dependencies beyond stdlib.
- Trivial to debug (`nc -U /run/hil-broker.sock`).
- Language-agnostic if we ever want a non-Python client.

Rejected:
- **gRPC / protobuf** — overkill, adds build step.
- **ZeroMQ** — nice, but another dependency for no concrete win on a single node.
- **HTTP on localhost** — tempting, but socket is simpler and lighter.

## RPC Surface (v1)

Start small. Every driver method that a current client uses becomes one RPC.

```
# SPI-side
adc.read(idx, channel) -> int
dac.set_voltage(idx, channel, volts) -> void
dac.get_voltage(idx, channel) -> float
can.set_mode(idx, mode) -> void
can.status(idx) -> {mode, tec, rec}
can.send(idx, frame) -> void
can.recv(idx, timeout) -> frame | null

# I2C-side
ina.read(addr) -> {bus_v, current, power}
tca.read(addr) -> {port0, port1}
tca.write_pin(addr, port, pin, value) -> void

# GPIO
psu.power(on: bool) -> void
psu.status() -> {ps_on, pwr_ok}

# Meta
broker.health() -> {uptime, bus_stats, last_error}
```

Frames are base64-encoded bytes. Errors come back as `{error: {code, message}}`.

## Migration Phases

### Phase 0 — Preparation (no behavior change)
- Create `docs/broker_migration_plan.md` (this file).
- Add `tools/hil_client.py` stub that *today* just re-exports the direct driver calls. This is the seam: flip its internals later without touching callers.
- Land a small PR. Merge to `main`.

### Phase 1 — Broker skeleton
- New package: `broker/` with `broker/server.py`, `broker/rpc.py`, `broker/bus.py`.
- Implement the RPC surface above, backed by the existing `tools/` drivers.
- Single-threaded, one lock per bus (SPI, I2C). GPIO does not need serialization but lives here for uniformity.
- Unit tests with a fake bus layer so CI can run without hardware.
- No client migration yet. Broker runs under a new systemd unit `hil-broker.service`; the existing agent/dashboard continue to bypass it.

### Phase 2 — Client cutover: dashboard
- Switch `tools/hil_client.py` internals from direct driver calls to broker RPC.
- Dashboard imports stay identical. Verify on hardware: 43/43 tests still pass, dashboard `/api/status` unchanged.
- Dashboard systemd unit gets `After=hil-broker.service` and `Requires=hil-broker.service`.

### Phase 3 — Client cutover: pytest suite
- `tests/hil/conftest.py` fixtures start talking through `hil_client.py`.
- Auto-skip logic now pings broker health instead of probing devices directly.
- The "don't run tests while dashboard is up" warning gets deleted from the memory file — this is the milestone.

### Phase 4 — Flasher as broker client
- The new CAN flasher (already written) is adapted to route MCP2515 access through the broker. All other flashers written from this point are broker-native.
- CI `hil-flash.yml` can now run concurrently with the dashboard without corruption.

### Phase 5 — Observability
- Broker exposes `/metrics` (bus op counts, latencies, error rates).
- Dashboard gains a "broker" panel showing queue depths and last errors.
- Log rotation via `journalctl` (already systemd-managed — no extra config).

## Testing Strategy

- **Unit**: broker RPC handlers, with driver layer mocked. Runs in CI, no hardware.
- **Integration (on RPi)**: full test suite in `tests/hil/` through the broker. Passes = Phase 2/3 done.
- **Stress**: a script that hammers the broker from N clients simultaneously and asserts no bus corruption (read-back DAC shadow, re-read ADCs, etc.).
- **Regression gate**: don't merge Phase 2 if dashboard latency regresses >20% versus direct access.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Broker becomes a single point of failure on the bench | systemd `Restart=on-failure`; clients reconnect with backoff; health endpoint |
| Added latency per call hurts dashboard 2s poll | Batch RPCs (`adc.read_all`, `ina.read_all`); measure before/after |
| Broker process crashes mid-flash and leaves DAC in bad state | Flash jobs hold a long-lived session; broker recovers known state on startup (re-apply DAC80504 init trio) |
| Two brokers accidentally start (e.g. dev + systemd) | PID file + socket bind check; refuse second start |
| Pytest and dashboard fight for CAN receive queues | RPC `can.recv` is owned by at most one subscriber at a time; others get `EBUSY` |

## What Changes for the Developer

- `sudo systemctl status hil-broker` is the new first thing to check.
- `journalctl -u hil-broker -f` replaces "tail the dashboard log for SPI errors."
- Standalone scripts in `tools/` that used to open `/dev/spidev*` directly are either retired or ported to be broker clients. Bring-up scripts stay direct-access but get a big comment: "stop the broker before running."
- The memory note "never run SPI scripts while the dashboard is up" is deleted once Phase 3 lands.

## Done Criteria

- `hil-broker.service` is the only process with `/dev/spidev*` open on the Pi (verified with `lsof`).
- Dashboard, pytest suite, and flasher all route through `tools/hil_client.py`.
- CI can run a flash job while the dashboard is live with no test or UI regression.
- `docs/architecture.md` is updated to reflect the new topology.
