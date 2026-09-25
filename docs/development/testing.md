# Testing guide

Four kinds of test live in the repo, serving distinct purposes:

| Suite | Location | Needs hardware? | When it runs |
|---|---|---|---|
| Broker unit / integration | [`tests/broker/`](../../tests/broker/) | No — uses fake backend | CI (`host-tests.yml`), and locally |
| Host-only guards | `tests/test_*.py` | No | CI (`host-tests.yml`), and locally |
| Bench self-tests | [`tests/hil/`](../../tests/hil/) (top-level `test_*.py`) | Yes — needs a live broker | By hand on the Pi; they flash nothing |
| DUT suites | `tests/hil/vcu/` (the ECU; the name is historical), `tests/hil/ams/` | Yes — drive a carrier and can **reflash** it | CI from a firmware PR ([below](#running-a-suite-from-a-firmware-pr)), or by hand under the bench lock |

`host-tests.yml` runs a whole-tree `--collect-only` plus everything
outside `tests/hil/`, on PRs that touch `tests/`, `tools/`,
`configs/`, `conftest.py` or `pyproject.toml` and on pushes to `dev`.
A change confined to `broker/` does not trigger it — run
`tests/broker/` yourself.

All of them use `pytest`, and all are designed so off-bench runs
degrade gracefully (broker and host tests pass on a laptop with no
hardware; HIL tests auto-skip when the broker socket is
unreachable).

---

## Running tests

### Broker unit tests (off-bench safe)

```sh
$ pytest tests/broker/ -v
# 15 passed in ~0.2s
```

These exercise:

- `tests/broker/test_rpc.py` — the JSON-RPC dispatcher against
  `FakeHardwareManager`. Exercises every method group except
  `i2c.*` (a cross-section, not every method-table entry),
  invalid-JSON handling, unknown methods, missing params,
  notifications (no `id`), and the op-counter increment.
- `tests/broker/test_server.py` — end-to-end socket round-trip
  via `BrokerClient`, with multiple concurrent clients and an
  error-surface smoke test.

### Host-only guards (off-bench safe)

```sh
$ python3 -m pytest tests/ --ignore=tests/hil -q    # what host-tests.yml runs
```

The top-level `tests/test_*.py` pin things that break silently
between bench runs: the bench descriptors, the named suites,
`flash_dut`, the recovery and watchdog logic, the Pico cell map and
NTC table, the workflow's build fallback.

### Bench self-tests (on-bench)

```sh
pi$ pytest tests/hil/ --ignore=tests/hil/vcu --ignore=tests/hil/ams -v
```

Expected: ~93 passed, ~11 skipped. Skips are for unpopulated
hardware (the nRF24L01+ isn't installed on the current boards, so
its test module skips). Without the two `--ignore`s pytest also
collects the DUT suites, which drive and can reflash carriers.

The suite runs concurrently with the dashboard without contention —
the broker serialises SPI/I²C across processes. Phase 3 was the
milestone that made this safe; the old "stop the dashboard before
running tests" rule is gone.

It does leave the bench changed, though. `test_can.py` ends with all
three `canN` links DOWN, last brought up with only a bitrate — so at
the kernel's default sample point, not the bench's 0.6875 — and
`test_spi_dac.py` soft-resets every DAC, which undoes the init the
broker wrote. Before any DUT run, put both back:

```sh
pi$ sudo systemctl restart hil-can-up hil-broker
```

Per-module invocation:

```sh
pi$ pytest tests/hil/test_spi_dac.py -v
pi$ pytest tests/hil/test_can.py -v -k loopback
```

### DUT suites by hand (on-bench)

Expand a named suite from
[`configs/suites.yaml`](../../configs/suites.yaml) exactly as CI does,
under the bench lock:

```sh
pi$ export AMS_FIRMWARE_BIN=/path/to/AMS.bin     # or ECU_FIRMWARE_BIN
pi$ flock /tmp/hil-bench.lock \
      pytest $(python3 -m tools.bench suite --dut ams --suite smoke) -v
```

- **The lock is not optional.** CI holds `/tmp/hil-bench.lock` for
  flash + pytest; it is the only thing that stops a dispatched run
  landing in the middle of your session.
- **Point the reflash fixture at your image.** Block A's A-003
  reflashes the carrier from `ECU_FIRMWARE_BIN` / `AMS_FIRMWARE_BIN`.
  Unset, the ECU suite falls back to `~/firmware-builds/ECU_fix.bin`
  (a stale 2026-06 diagnostic build on bench-01) and every later case
  reports on that; the AMS one looks for `/tmp/AMS.bin`.

---

## How the fake backend works

`broker/fake_bus.py` implements `HardwareBackend` in memory:

- Stores ADC and DAC values in plain Python lists.
- Exposes the same method names as `HardwareManager`, with the
  same return shapes.
- Tracks an op counter the way the real backend does, so tests
  that assert on `broker.health` work identically.
- Has a small amount of sanity checking (e.g. `ina.read` raises
  `KeyError` on an unknown address), so negative tests can hit
  reasonable error paths.

To add a new RPC method:

1. Add the method to `HardwareBackend` (the `Protocol` in
   `broker/bus.py`).
2. Implement on `HardwareManager` in `broker/bus.py` (real
   hardware behaviour, wrapped in the appropriate per-bus lock).
3. Implement on `FakeHardwareManager` in `broker/fake_bus.py`
   (in-memory stub).
4. Register in `broker/rpc.py`'s `build_method_table`.
5. Add a test in `tests/broker/test_rpc.py` that exercises it via
   `handle_request`.

If the method is user-facing (called from the dashboard or HIL
tests), also add the proxy to `tools/hil_client.py` and document
it in `docs/broker-api.md`.

---

## HIL test structure

`tests/hil/conftest.py` defines session-scoped fixtures that
return broker proxies:

- `broker_available` (autouse) — pings `broker.health` once per
  session; the whole suite skips if the socket isn't reachable.
- `psu_on` — asserts `PS_ON#` and waits for `PWR_OK`.
- Per-device fixtures (`adcs`, `dacs`, `can_controllers`,
  `power_monitors`, `io_expanders`, `nrf24`) return proxy
  instances.
- Compatibility shims `spi_bus` and `i2c_bus` yield `None` (broker
  owns the real handles) for tests still taking them as
  parameters.

The bench self-tests (top-level `tests/hil/test_*.py`) are grouped by
subsystem. The DUT suites under `tests/hil/vcu/` and `tests/hil/ams/`
add their own `conftest.py` and a DUT profile (`vcu_profile.yaml`,
`ams_profile.yaml`).

| File | Covers |
|---|---|
| `test_example.py` | Trivial smoke / placeholder |
| `test_spi_adc.py` | MCP3208 channel reads, stuck-bus detection |
| `test_spi_dac.py` | DAC80504 register I/O + channel sweep |
| `test_can.py` | MCP2515 reset, init, loopback TX/RX, link-health |
| `test_i2c.py` | `i2c.scan`, INA226 ID/measurement, TCA9555 I/O |
| `test_mlc_power.py` | Per-carrier INA226 readings |
| `test_relays.py` | Relay energise / de-energise via TCA9555 |
| `test_nrf24.py` | nRF24 presence + config (auto-skip when absent) |

### Adding a new HIL test

1. Pick or create the appropriate module.
2. Declare fixtures you need (from `conftest.py`).
3. Use the proxy objects from `tools.hil_client` — never
   `spidev` / `smbus2` / `RPi.GPIO` directly.
4. Make the test auto-skip if it needs a physical thing that's
   optional. Pattern:
   ```python
   @pytest.fixture(autouse=True)
   def skip_if_no_X(X):
       if not X.is_present():
           pytest.skip("X not responding — skipping")
   ```
5. Run locally against the fake backend where feasible
   (`python3 -m broker.server --fake --socket /tmp/hil-broker.sock`
   plus `HIL_BROKER_SOCKET=/tmp/hil-broker.sock`; see
   [`setup.md`](setup.md)), then on the bench.

---

## Running a suite from a firmware PR

You do not need a bench login to test firmware. From a PR in
`IFS08-CE-AMS` or `IFS08-CE-ECU`, either entry point dispatches a run:
it builds that PR's commit on a cloud runner, flashes the right carrier,
runs the suite, and posts the result back as a PR comment naming the
failing cases.

| Entry point | How | Which copy of the workflow runs |
|---|---|---|
| **Label** | add the `hil-test` label to the PR | the PR's **own** tree — so a change to the workflow can be tested in the PR that makes it |
| **Comment** | comment `/hil-test` on the PR | always the **default branch's** copy |

The comment path's behaviour is a GitHub constraint on `issue_comment`,
not a bug: such workflows always run from the default branch, so
`hil-test.yml` has to reach `main` on the firmware repo before `/hil-test`
fires at all. The label path has no such constraint.

### Picking what runs

A bare trigger runs `smoke`. Everything else is opt-in by name, and a
pytest path is passed straight through:

```sh
/hil-test                                      # smoke (the default)
/hil-test dv                                   # a named suite
/hil-test full                                 # everything for that DUT
/hil-test tests/hil/vcu/test_block_c_fsm.py    # a path, unchanged
```

The names come from [`configs/suites.yaml`](../../configs/suites.yaml),
which is the source of truth — this table will drift, that file will not:

| DUT | Suite | Covers |
|---|---|---|
| `ecu` | `smoke` | boot, BL discover, flash+jump, bus independence, 0x100 heartbeat, AMS gate, 0x704 health |
| | `full` | all of `tests/hil/vcu/` |
| | `dv` | driverless block (needs the AMS powered) |
| | `fsm` | state machine |
| | `inverter` | inverter + fault recovery (bench-01 has none) |
| | `telemetry` | telemetry + pit-diag |
| `ams` | `smoke` | Block A boot |
| | `full` | all of `tests/hil/ams/` |
| | `balancing` | cell balancing |
| | `safety` | safety predicates |
| | `relays` | relay driver |

`smoke` is the default because it is the one suite that must pass
unattended on a healthy bench. Adding a case to it is a promise to that
effect; a case needing hardware the bench descriptor does not declare
belongs in a named suite instead.

### Running it by hand

Actions → *HIL bench test* → **Run workflow**. `suite` takes the same
values as above. Also useful: `bench` pins a bench id, `capabilities`
matches by capability instead, `pytest_args` passes extra pytest flags,
`comment_on` + `comment_repo` post the result to a firmware PR, and
`allow_bench_build` lets the bench compile the firmware itself when the
artifact quota blocks the cloud upload. Leave `allow_degraded` alone —
it runs the suite on a bench that failed its own preflight.

### Before blaming the firmware

Some reds belong to the bench. Check the suite's own notes and the open
issues first — `configs/suites.yaml` records which cases bench-01
structurally cannot pass (no inverter; `flash_dut` de-energises MLC2 to
flash the ECU, so cases needing a live AMS fail in a dispatched ECU run).

One CI failure mode is worth knowing because it is silent. An invalid
expression in a workflow is a *startup* failure: GitHub cannot parse the
file, so the run gets zero jobs, no logs, and **no check-run** — which
means `gh pr checks` reads green while nothing ran. This hid a dead
`hil-test.yml` for three weeks. Zero jobs means it never started:

```sh
$ gh api repos/isc-fs/IFS_HIL/actions/runs/<id>/jobs -q .total_count
```

---

## Reading test output

### Successful bench self-test run

```
tests/hil/test_can.py::TestMCP2515Reset::test_reset_enters_config_mode[CAN1 (U17)] PASSED
tests/hil/test_can.py::TestMCP2515Reset::test_reset_enters_config_mode[CAN2 (U19)] PASSED
…
93 passed, 11 skipped in 3.22s
```

Skips you should see:

- `tests/hil/test_nrf24.py` — entire module skipped (nRF not
  populated).
- A handful of INA226 `test_bus_voltage_positive` cases —
  intentionally skipped because the board's INA226s are low-side
  sensing and `bus_voltage` always reads ~0 V.

### "Everything skipped"

Means the broker socket isn't reachable:

```
104 skipped in 0.5s
```

Check `ls /run/hil-broker/broker.sock`,
`systemctl status hil-broker`, and that `HIL_BROKER_SOCKET` isn't
set to a stale path. See
[`../troubleshooting.md`](../troubleshooting.md).

---

## Read these next

- [`setup.md`](setup.md) — dev environment & branch policy.
- [`kernel-module.md`](kernel-module.md) — iterating on
  `mcp251x-patched`.
- [`../broker-api.md`](../broker-api.md) — the RPC surface your
  tests call.
