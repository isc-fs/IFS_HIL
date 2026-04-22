# Broker RPC reference

`hil-broker` exposes a JSON-RPC surface over a Unix socket at
`/run/hil-broker/broker.sock`. Every method in this document is
dispatched by [`broker/rpc.py`](../broker/rpc.py); the real-hardware
and fake backends that satisfy the method table live in
[`broker/bus.py`](../broker/bus.py) and
[`broker/fake_bus.py`](../broker/fake_bus.py).

This document is the authoritative client reference. If an operator
or a test asks "what does `dac.zero_all` do, exactly?", this is the
answer.

---

## Transport

- **Socket**: Unix stream socket at
  `/run/hil-broker/broker.sock` (default; overridable via the
  `HIL_BROKER_SOCKET` environment variable).
- **Framing**: newline-delimited JSON. One request per line, one
  response per line.
- **Request**:
  ```json
  {"id": <any>, "method": "dac.set_voltage", "params": {"idx": 0, "channel": 2, "volts": 1.5}}
  ```
- **Success response**:
  ```json
  {"id": <same>, "result": <any>}
  ```
- **Error response**:
  ```json
  {"id": <same>, "error": {"code": "<string>", "message": "<string>"}}
  ```
- **Notifications**: requests without an `id` field get no reply.

Byte-array parameters (CAN payloads) are base64-encoded strings.

### Client helper

The Python client is `broker.server.BrokerClient`:

```python
from broker.server import BrokerClient
c = BrokerClient('/run/hil-broker/broker.sock')
c.call('dac.set_voltage', idx=0, channel=2, volts=1.5)
val = c.call('adc.read_voltage', idx=1, channel=3)
c.close()
# or:
with BrokerClient(path) as c:
    c.call(...)
```

One socket per `BrokerClient` instance. Not thread-safe — create
one per thread.

### Error codes

| Code | Meaning |
|---|---|
| `parse_error` | Line was not valid JSON. |
| `invalid_request` | JSON was valid but missing `method` or had non-object `params`. |
| `method_not_found` | No such method registered. |
| `invalid_params` | Method doesn't accept the given parameters (wrong names / missing). |
| `internal_error` | Driver call raised; `message` carries the exception type + text. |

---

## Method groups

- [`adc.*`](#adc-methods) — MCP3208 analog inputs
- [`dac.*`](#dac-methods) — DAC80504 analog outputs
- [`can.*`](#can-methods) — MCP2515 CAN controllers (kernel-backed)
- [`ina.*`](#ina-methods) — INA226 power monitors
- [`tca.*`](#tca-methods) — TCA9555 I/O expanders
- [`nrf.*`](#nrf-methods) — nRF24L01+ (presence check only)
- [`i2c.*`](#i2c-methods) — raw I²C bus scan
- [`psu.*`](#psu-methods) — ATX PSU control
- [`broker.*`](#broker-methods) — metadata

Indices (`idx`) for ADC / DAC / CAN:

| `idx` | ADC (MCP3208) | DAC (DAC80504) | CAN (kernel netdev) |
|---:|---|---|---|
| `0` | U9 — ADC1 | U12 — DAC1 | `can0` (PCB CAN3) |
| `1` | U10 — ADC2 | U13 — DAC2 | `can1` (PCB CAN2) |
| `2` | U11 — ADC3 | U14 — DAC3 | `can2` (PCB CAN1) |
| `3` | — | U15 — DAC4 | — |

See [`hardware-reference.md`](hardware-reference.md) for addresses
used by `ina.*`, `tca.*`, and the CAN netdev ↔ PCB inversion.

---

## ADC methods

### `adc.read`

Read a raw 12-bit sample from one MCP3208 channel.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |
| `channel` | int | 0–7 |

**Returns** `int` — raw sample 0..4095.

### `adc.read_voltage`

Same as `adc.read` but scaled to volts by the driver (VREF = 3.3 V).

**Returns** `float` — volts.

### `adc.read_all`

Read all 8 channels of one ADC back-to-back.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |

**Returns** `list[int]` — eight raw samples 0..4095.

---

## DAC methods

### `dac.set_voltage`

Drive one channel of one DAC80504 to the requested voltage.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2, 3 |
| `channel` | int | 0–3 |
| `volts` | float | 0.0 .. 3.3 (clipped) |

**Returns** `null`.

### `dac.get_voltage`

Read back the shadow INPUT register of one channel.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2, 3 |
| `channel` | int | 0–3 |

**Returns** `float` — volts.

**Note**: this reads the chip's digital input register, *not* the
actual analog output. Use `adc.read_voltage` on a wire-looped
ADC channel to verify the physical output.

### `dac.read_device_id`

Reads the chip DEVID register. Mostly a health check.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2, 3 |

**Returns** `int` — `0x0417` on a healthy DAC80504.

### `dac.reset`

Issue a software reset to one DAC.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2, 3 |

**Returns** `null`.

### `dac.zero_all`

Set every channel of one DAC to 0 V.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2, 3 |

**Returns** `null`.

---

## CAN methods

All CAN methods are backed by the kernel SocketCAN interface
`canN`; the broker manages link state with `ip link set …` (via
the sudoers drop-in) and frame I/O with `python-can`.

Mode byte encoding — preserved for backwards compatibility with
the legacy register-level driver callers:

| Byte | Kernel state | Semantics |
|---|---|---|
| `0x80` | link DOWN | Historical MCP2515 CONFIG mode |
| `0x00` | link UP, `loopback off` | Normal CAN operation |
| `0x40` | link UP, `loopback on` | Chip internal loopback |

### `can.set_mode`

Transition the interface to the requested mode. Closes the current
`python-can` Bus (if any), runs `ip link set` to re-up the
interface at the stored bitrate with or without loopback, and
records the new mode.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |
| `mode` | int | `0x80` / `0x00` / `0x40` |

**Returns** `bool` — `true` on success.

### `can.get_mode`

Reflects kernel state: DOWN → `0x80`, otherwise whatever
`can.set_mode` last recorded.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |

**Returns** `int`.

### `can.status`

Convenience: returns mode + both error counters in one call.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |

**Returns** `dict`:
```json
{"mode": 0, "tec": 0, "rec": 0}
```

### `can.read_error_counters`

Parses `ip -s -d -j link show canN` and extracts the kernel's
`berr-counter` `{tx, rx}` pair.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |

**Returns** `list[int]` — `[tec, rec]`.

### `can.reset`

Force the interface to CONFIG (link DOWN). Equivalent to
`can.set_mode(idx, 0x80)` but semantically "reset state".

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |

**Returns** `null`.

### `can.init`

Legacy bring-up entry point. Records the bitrate and leaves the
interface DOWN. Caller then uses `can.set_mode` to pick
normal or loopback.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |
| `bitrate` | int | bits/second, e.g. `500000` |

**Returns** `bool` — `true`.

Bringing the chip up in NORMAL mode with no CAN peer drives it
straight into BUS-OFF, so `can.init` deliberately stays DOWN and
lets the caller decide.

### `can.loopback_test`

Put the chip in LOOPBACK, send one frame, read one frame back,
compare. Used by the HIL test suite.

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |
| `can_id` | int | 11-bit ID |
| `data_b64` | str | base64-encoded payload (≤ 8 bytes) |

**Returns** `bool` — `true` if the received frame matched.

### `can.int_level`

Historically read the MCP2515 INT GPIO pin; under the kernel
driver those pins are unreadable from userspace. The broker
repurposes this method as a link-health proxy:

- returns `1` if the interface is UP (healthy)
- returns `0` if the interface is DOWN or unreachable

| Param | Type | Description |
|---|---|---|
| `idx` | int | 0, 1, 2 |

**Returns** `int` — 0 or 1.

---

## INA methods

All `ina.*` methods take an `addr` parameter that selects one of
the four INA226s on the bus. Valid addresses:

| `addr` | MLC carrier |
|---:|---|
| `0x40` | MLC1 |
| `0x41` | MLC2 |
| `0x44` | MLC3 |
| `0x45` | MLC4 |

### `ina.read`

Full snapshot — one SPI transaction per field.

| Param | Type | Description |
|---|---|---|
| `addr` | int | INA226 I²C address |

**Returns** `dict`:
```json
{"bus_voltage_V": 0.005, "shunt_voltage_V": 0.0013,
 "current_A": 0.135, "power_W": 0.0}
```

Because the sensing is low-side, `bus_voltage_V` is always near 0;
only `current_A` (and `shunt_voltage_V` × 100 for mA) is meaningful.

### `ina.is_present`

Verify the manufacturer and die IDs match Texas Instruments' INA226.

| Param | Type | Description |
|---|---|---|
| `addr` | int | INA226 I²C address |

**Returns** `bool`.

### `ina.bus_voltage`, `ina.shunt_voltage`, `ina.current`, `ina.power`

Single-register read shortcuts. Each takes `addr`, returns a
`float` in V / V / A / W.

### `ina.read_manufacturer_id`, `ina.read_die_id`

Return `int`. Useful for chip presence + identity checks.

---

## TCA methods

All `tca.*` methods take an `addr` parameter:

| `addr` | Ref | Role |
|---:|---|---|
| `0x20` | U3 | Relay coil drivers (port 0 bits 0–3 = K1–K4) |
| `0x21` | U6 | Slot LED indicators (various pins) |
| `0x22` | U8 | Other I/O |

Each TCA9555 has two 8-bit ports (`0`, `1`), 8 pins each.

### `tca.read`

Return a full snapshot of the chip (all four registers per port).

**Returns** `dict`:
```json
{"input_port0": …, "input_port1": …,
 "output_port0": …, "output_port1": …,
 "config_port0": …, "config_port1": …}
```

### `tca.is_present`

ACK test. Returns `bool`.

### `tca.read_port`

Read just the live input register of one port.

**Returns** `int` — 0..255.

### `tca.set_direction`

Set per-pin direction on one port.

| Param | Type | Description |
|---|---|---|
| `addr` | int | |
| `port` | int | 0 or 1 |
| `mask` | int | bitmask: `1` = input, `0` = output |

### `tca.get_direction`

Read back the CONFIG register of one port.

**Returns** `int` — same bitmask semantics as `tca.set_direction`.

### `tca.set_all_inputs`, `tca.set_all_outputs`

Shortcuts — configure every pin on both ports as input or output.

### `tca.write_port`

Drive every pin on one port simultaneously.

| Param | Type | Description |
|---|---|---|
| `addr` | int | |
| `port` | int | 0 or 1 |
| `value` | int | 0..255 |

### `tca.write_all`

Drive both ports in one call.

| Param | Type | Description |
|---|---|---|
| `addr` | int | |
| `p0` | int | port 0 value |
| `p1` | int | port 1 value |

### `tca.write_pin`

Drive one bit on one port.

| Param | Type | Description |
|---|---|---|
| `addr` | int | |
| `port` | int | 0 or 1 |
| `pin` | int | 0–7 |
| `value` | bool | |

---

## nRF methods

### `nrf.is_present`

Probe the (unpopulated) nRF24L01+. Returns `bool` — typically
`false` on this board.

---

## I²C methods

### `i2c.scan`

Iterate addresses `[start, end)` and report the ones that ACK a
`write_byte`. Used by `tests/hil/test_i2c.py`.

| Param | Type | Description |
|---|---|---|
| `start` | int | first address, default `0x08` |
| `end` | int | one past last, default `0x78` |

**Returns** `list[int]` — responding addresses.

On a healthy BACKPLANE_HIL bench you should see exactly 7: four
INA226 + three TCA9555.

---

## PSU methods

### `psu.power`

Assert or de-assert `PS_ON#` (GPIO7). When asserting, polls
`PWR_OK` (GPIO8) for up to 5 s.

| Param | Type | Description |
|---|---|---|
| `on` | bool | |

**Returns** `dict`:
```json
{"ps_on": true, "pwr_ok": true}
```

### `psu.status`

Read the current state of both signals.

**Returns** same shape as `psu.power`.

---

## Broker methods

### `broker.health`

Returns uptime, total RPC op count, and the last error message if
any.

**Returns** `dict`:
```json
{"uptime_s": 1234.56, "op_count": 987, "last_error": null}
```

The real backend also reports `backend: "real"`-style metadata;
the fake backend reports `"backend": "fake"`. Useful for
distinguishing on-bench from off-bench in tests.

---

## Notes on threading

- The broker serialises SPI transactions behind one lock, I²C
  behind another, and CAN link-state changes behind a third.
- Multiple RPCs on **different** buses can run concurrently.
- `can.loopback_test` holds the CAN lock for the duration of the
  test (~200 ms with default timeouts). During that window, other
  CAN RPCs block. Non-CAN RPCs are unaffected.
