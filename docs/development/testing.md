# Testing guide

Two test suites live in the repo, serving distinct purposes:

| Suite | Location | Needs hardware? | When it runs |
|---|---|---|---|
| Broker unit / integration | [`tests/broker/`](../../tests/broker/) | No — uses fake backend | Every PR, CI-safe |
| HIL on-bench | [`tests/hil/`](../../tests/hil/) | Yes — needs a live broker | Pre-merge on the Pi, and in CI once the runner is wired |

Both suites use `pytest`. Both are designed so off-bench runs
degrade gracefully (broker tests pass on a laptop with no
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
  `FakeHardwareManager`. Covers every method-table entry,
  invalid-JSON handling, unknown methods, missing params,
  notifications (no `id`), and the op-counter increment.
- `tests/broker/test_server.py` — end-to-end socket round-trip
  via `BrokerClient`, with multiple concurrent clients and an
  error-surface smoke test.

### HIL tests (on-bench)

```sh
pi$ pytest tests/hil/ -v
```

Expected: ~93 passed, ~11 skipped. Skips are for unpopulated
hardware (the nRF24L01+ isn't installed on the current boards, so
its test module skips).

The suite runs concurrently with the dashboard without contention —
the broker serialises SPI/I²C across processes. Phase 3 was the
milestone that made this safe; the old "stop the dashboard before
running tests" rule is gone.

Per-module invocation:

```sh
pi$ pytest tests/hil/test_spi_dac.py -v
pi$ pytest tests/hil/test_can.py -v -k loopback
```

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

Test files are grouped by subsystem:

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
5. Run locally against the fake backend where feasible, then on
   the bench.

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
/hil-test full                                 # everything for that DUT but soak cases
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
| | `full` | all of `tests/hil/ams/` except the [soak cases](#soak-cases-are-opt-in) |
| | `balancing` | cell balancing |
| | `safety` | safety predicates |
| | `relays` | relay driver |

`smoke` is the default because it is the one suite that must pass
unattended on a healthy bench. Adding a case to it is a promise to that
effect; a case needing hardware the bench descriptor does not declare
belongs in a named suite instead.

#### Soak cases are opt-in

A case marked `soak` never runs unless you ask for it: not in `full`,
not from a path, not even when you name it outright. `pyproject.toml`
sets `addopts = "-m 'not soak'"`, so every pytest run in this repo
deselects them, on the bench, in CI and on a laptop. They are the AMS
endurance rows, and some of them are destructive:

- **Block F** (`test_block_f_flash_endurance.py`), F-070…F-081:
  reset/flash/boot endurance, up to 1000 cycles a row. **F-077 cuts
  carrier power mid-flash**, ten times. An interrupted flash has left
  an STM32H7 unrecoverable over CAN before
  ([stm32-can-bootloader#166](https://github.com/isc-fs/stm32-can-bootloader/issues/166)).
- **Block G** (`test_block_g_soak.py`): E-050 and E-051 (30 min
  each) and E-052 (50 power cycles). G-097 and G-102 in the same file
  are quick and are not soak.
- **`test_block_can1m.py`**: M-05 (reboot + reflash) and M-06 (a
  5-minute soak).

`pytest tests/hil/ams/ -m soak --collect-only -q` lists them.

To run one, pass `-m soak` and narrow it down. `-m` is last-wins, so
yours replaces the default rather than adding to it. That cuts both
ways: *any* `-m` you pass brings the soak cases back unless it also
says `and not soak`. `--soak-scale` shrinks durations and cycle counts
(0.1 turns a 30-minute soak into 3 minutes, 100 cycles into 10). Block F
reflashes from `AMS_FIRMWARE_BIN` (default `/tmp/AMS.bin`), so stage the
image you mean it to write.

```sh
pi$ flock /tmp/hil-bench.lock \
      pytest tests/hil/ams/test_block_g_soak.py -m soak --soak-scale 0.1 -v
```

Run them by hand, on a bench you are watching. Through CI, keep it to
one scaled case: `suite` = `tests/hil/ams/test_block_f_flash_endurance.py`,
`pytest_args` = `-m soak -k f070 --soak-scale 0.1`. The test job's
`timeout-minutes: 90` kills the run wherever it is, and in Block F that
can be the middle of a flash.
[`tests/test_soak_is_opt_in.py`](../../tests/test_soak_is_opt_in.py)
fails host CI if a soak case ever reaches the default run.

### Running it by hand

Actions → *HIL bench test* → **Run workflow**. `suite` takes the same
values as above. Also useful: `bench` pins a bench id, `capabilities`
matches by capability instead, `pytest_args` passes extra pytest flags
(`-m soak` included — [read the above first](#soak-cases-are-opt-in)),
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

### Successful HIL run

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
93 skipped in 0.5s
```

Check `ls /run/hil-broker/broker.sock` and
`systemctl status hil-broker`. See
[`../troubleshooting.md`](../troubleshooting.md).

---

## Read these next

- [`setup.md`](setup.md) — dev environment & branch policy.
- [`kernel-module.md`](kernel-module.md) — iterating on
  `mcp251x-patched`.
- [`../broker-api.md`](../broker-api.md) — the RPC surface your
  tests call.
