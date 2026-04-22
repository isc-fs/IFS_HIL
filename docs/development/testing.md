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

## CI state (today)

- Unit-test CI against the fake backend is **not yet wired**. A
  GitHub Actions job running `pytest tests/broker/` on every PR
  is a known follow-up — straightforward, just not done yet.
- HIL on-bench CI requires the Pi as a self-hosted runner. The
  scaffolding lives in `.github/workflows/` (`hil-build-trigger`,
  `hil-build-only`, `hil-flash`), but wiring `pytest tests/hil/`
  in after a flash is Phase 5+ territory.

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
