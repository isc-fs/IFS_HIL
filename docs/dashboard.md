# Dashboard reference

Lightweight Flask web UI for inspecting and controlling the
BACKPLANE_HIL bench. Serves a single-page dark-themed UI with two
tabs — **Bench** and **CAN trace** — and a JSON API. The Bench tab
and `/api/status` read from an in-memory cache the dashboard
maintains by polling the broker every 2 s; the CAN trace reads
SocketCAN directly.

Source: [`dashboard/app.py`](../dashboard/app.py),
[`dashboard/can_tracer.py`](../dashboard/can_tracer.py) and
[`dashboard/index.html`](../dashboard/index.html).

---

## Launching the dashboard

The dashboard is a systemd service. Once installed it starts at boot
and follows `hil-broker.service` in the dependency chain — see
[`infra/systemd/hil-dashboard.service`](../infra/systemd/hil-dashboard.service)
and [`infra/systemd/README.md`](../infra/systemd/README.md).

Day-to-day:

```sh
pi$ systemctl status hil-dashboard       # state + last log lines
pi$ sudo systemctl restart hil-dashboard # after editing dashboard code
pi$ sudo systemctl stop hil-dashboard    # bring it down
pi$ journalctl -u hil-dashboard -f       # follow logs live
```

For ad-hoc runs (different port, debugging, off-bench dev), launch
manually instead — stop the service first to free port 8080:

```sh
pi$ sudo systemctl stop hil-dashboard
pi$ cd ~/IFS_HIL
pi$ python3 dashboard/app.py --port 8080 --poll-interval 2.0
```

The dashboard needs a reachable broker socket. Default:
`/run/hil-broker/broker.sock`, overridable via `HIL_BROKER_SOCKET`.
With `hil-broker.service` running, it's just there.

**Starting the dashboard powers off every carrier.** On start-up it
writes TCA9555 `0x20` port 0 to `0x00`, de-energising K1–K4 — on
`systemctl start` / `restart`, on a crash-restart
(`Restart=on-failure`), and on a manual `python3 dashboard/app.py`.
Don't start or restart it while a carrier is being flashed or
tested.

---

## Web UI (`/`)

**Bench** tab layout:

- **Header** — connection state and the timestamp of the last
  successful poll.
- **PSU** — `PWR_OK` indicator and a toggle that calls
  `/api/psu/power`.
- **Carriers** — one row per MLC slot (MLC1..MLC4) with:
  - Relay-on toggle (calls `/api/carrier/<slot>/power`). It shows
    what the dashboard last switched, not the relay's actual state —
    relays switched by tests, `flash_dut` or CI don't show here.
  - Live INA226 current reading in mA. Overcurrent rows
    highlight red when `|current| > MLC_CURRENT_MAX_A` (3 A
    default).
  - INA226 power reading in mW and shunt voltage in mV.
  - Carrier-present indicator (from INA226 presence check).
- **CAN** — one row per MCP2515 with:
  - Current operating mode, as `can.get_mode` reports it — a link
    that `hil-can-up` brought up shows `config` until a mode is set
    from here (see [`broker-api.md`](broker-api.md#can-methods)).
  - Live TEC/REC counters.
  - Mode-change dropdown that calls `/api/can/<idx>/mode`.
  - **The row labels are inverted.** Rows are in broker index
    order — kernel `can0`, `can1`, `can2` — but labelled CAN1 (U17),
    CAN2 (U19), CAN3 (U21). The row marked "CAN1 (U17)" is `can0` =
    PCB CAN3, and the carrier bus (`can2` = PCB CAN1, U17) is the row
    marked "CAN3 (U21)". See the
    [kernel netdev mapping](hardware-reference.md#can-netdev--pcb-label-mapping-crucial).
- **DAC** — one row per DAC80504 with per-channel setpoint inputs
  that post to `/api/dac/<idx>/channel/<ch>`. Shows the last-set
  voltage (read from the shadow register).
- **ADC** — one row per MCP3208, showing all 8 channels' live
  voltages.
- **I/O expanders** — one row per TCA9555 with port0 and port1
  values in hex, per-pin state indicators.
- **nRF24L01+ (U23)** — presence (not populated on bench-01).

The **CAN trace** tab shows every frame on `can0`–`can2`: it
backfills the last 200 from `/api/can/recent`, then follows
`/api/can/stream`, with per-bus checkboxes, an ID filter, pause and
clear. AMS `0x4A0`–`0x4A2` and `0x100` frames are decoded.

---

## HTTP API

All endpoints return JSON. Control endpoints accept
`application/json` bodies with the shown shape.

### `GET /`

Serves the `index.html` UI.

### `GET /api/status`

Returns the most recent cached poll result:

```json
{
  "timestamp": "2026-04-22T01:45:12+00:00",
  "psu": {"ok": true, "on": true},
  "can": [
    {"name": "CAN1 (U17)", "ok": true, "mode": "normal",
     "tec": 0, "rec": 0},
    …
  ],
  "adc": [
    {"name": "ADC1 (U9)", "ok": true,
     "channels": [0.003, 0.005, …]},
    …
  ],
  "dac": [
    {"name": "DAC1 (U12)", "ok": true,
     "channels": [0.0, 1.5, 0.0, 0.0]},
    …
  ],
  "power": [
    {"name": "MLC1", "ok": true, "present": true,
     "current_mA": 134.77, "power_mW": 665.48,
     "shunt_mV": 1.33, "relay_on": true,
     "overcurrent": false},
    …
  ],
  "io": [
    {"name": "U3 (0x20)", "ok": true, "present": true,
     "port0": 5, "port1": 0},
    …
  ],
  "nrf24": {"present": false}
}
```

The poll loop updates this cache every 2 s (configurable via
`--poll-interval`). `/api/status` never blocks on hardware — it
always returns the most recent snapshot.

`can[i]` is kernel `can<i>` (so its `name` label is inverted — see
[Web UI](#web-ui-)). `psu.ok` is the live `PWR_OK`, but `psu.on` and
each `relay_on` are the dashboard's own records, not hardware reads:
set at start-up, then changed only by its own POSTs.

### `POST /api/psu/power`

Toggle the ATX PSU.

**Body**: `{"on": true}` or `{"on": false}`.

**Success**: `{"on": true}` (200).

**Failure modes**:
- 500 with `{"error": "PWR_OK not asserted after 5 s"}` —
  asserted `PS_ON#` but the ATX never signalled `PWR_OK`. Usually
  means no ATX connected or the supply isn't on at the wall.

Turning the PSU off also resets the in-memory relay-state cache
to all-off (because the 12 V rail driving the coils goes away).

### `POST /api/carrier/<int:slot>/power`

Energise or de-energise one MLC relay. `slot` is 1..4.

**Body**: `{"on": true}` or `{"on": false}`.

**Success**: `{"slot": 1, "on": true}` (200).

**Failure modes**:
- 400 if `slot` is out of range.
- 500 with `{"error": "<detail>"}` on a relay I/O failure.

Internally this maps to the TCA9555 pin per
[hardware-reference.md → carrier relay map](hardware-reference.md#carrier-relay-map).

### `POST /api/dac/<int:idx>/channel/<int:ch>`

Set one DAC80504 channel to a voltage.

**URL**: `idx` 0..3 (chip), `ch` 0..3 (channel).

**Body**: `{"voltage": 1.5}`.

**Success**: `{"idx": 0, "channel": 2, "voltage": 1.5}` (200).

**Failure modes**:
- 400 for out-of-range indices or a missing/invalid `voltage` key.
- 500 on SPI transfer failure.

### `POST /api/tca/<int:addr>/port/<int:port>/pin/<int:pin>`

Drive one TCA9555 pin. `addr` is the I²C address, written in
decimal in the URL (`32`, `33` or `34` for 0x20–0x22); `port` is 0
or 1, `pin` is 0..7.

**Body**: `{"value": true}` or `{"value": false}`.

**Success**: `{"addr": 32, "port": 0, "pin": 5, "value": true}`
(200).

**Failure modes**:
- 400 for unknown address or out-of-range port / pin.
- 500 on I²C failure.

This endpoint also ensures the port's direction is set to
outputs before writing, which makes it safe to call on a freshly-
powered bench.

### `POST /api/can/<int:idx>/mode`

Change one MCP2515's mode (via the broker's `can.set_mode`).

**URL**: `idx` 0..2.

**Body**: `{"mode": "normal"}` — one of `"normal"`,
`"loopback"`, `"listenonly"`, `"config"`, `"sleep"` — mapped to
the legacy MCP2515 byte values before passing to the broker.

**Success**: `{"idx": 0, "mode": "loopback"}` (200).

**Failure modes**:
- 400 for an unknown mode string.
- 500 on broker error; `{"error": "mode change timed out"}`
  if the broker returned `False`.

The broker has only three link states: `config` takes the link down,
`loopback` brings it up in loopback, and everything else — `sleep`
and `listenonly` included — brings it up in normal mode.

Any change re-ups the interface with only a bitrate, so the kernel
falls back to its default sample point (0.875) instead of the
bench's 0.6875. On `idx` 2 — kernel `can2`, the carrier bus — the
bench drops off the bus (`config`) or comes back at a sample point
the DUTs bus-off against. Don't touch it while a carrier is under
test; `sudo systemctl restart hil-can-up` puts it back.

### `GET /api/can/recent`

Snapshot of the CAN trace ring buffer (the last 1000 frames per bus).

**Query**: `bus` (`can0` / `can1` / `can2`; omit for all buses,
merged by time) and `n` (default 200, clamped to 1..2000).

**Success**: `{"frames": [<frame>, …]}` (200), each frame:

```json
{"seq": 1234, "ts_ms": 1742394283417, "bus": "can2",
 "id": "0x4A0", "id_int": 1184, "ext": false, "dlc": 8,
 "data": "0500 1F07 0EA6 0EA6", "decoded": {"kind": "ams_telem_status", …}}
```

`decoded` is filled for AMS `0x4A0` / `0x4A1` / `0x4A2` and `0x100`,
`null` otherwise. Kernel error frames are dropped.

**Failure**: 503 `{"error": "CAN tracer not started"}`.

### `GET /api/can/stream`

Server-Sent Events: one `data:` event per frame (same shape as
above) on any bus as it arrives, plus a `: keepalive` comment every
~15 s. A client that falls 512 frames behind stops receiving frames
and has to reconnect. 503 if the tracer isn't running.

The tracer opens its own raw SocketCAN socket per interface — not
through the broker (AF_CAN sockets aren't the `/dev` nodes the broker
owns) — and binds each once, at start-up: a bus it could not bind
then stays untraced until the dashboard restarts.

---

## Internal poll loop

`_poll()` runs every `--poll-interval` seconds (default 2.0) on a
background thread. Per cycle:

- `psu.status`; `can.get_mode` and `can.read_error_counters` per
  CAN chip; `adc.read_voltage` for each of 8 channels × 3 ADCs;
  `dac.get_voltage` for 4 channels × 4 DACs; `ina.is_present`,
  `current`, `power` and `shunt_voltage` per carrier;
  `tca.is_present` and two `read_port`s per expander;
  `nrf.is_present`. About 73 broker RPCs.
- Result is cached under a `_state_lock`. `/api/status` copies
  out of that cache.

The broker serialises all of these, so the dashboard never
conflicts with a parallel `pytest tests/hil/` run or an ad-hoc
Python session. That's the whole reason the broker exists.

---

## Relationship to the broker

The dashboard is a broker client, identical in every way to how
`tests/hil/` uses the broker — it imports
`tools.hil_client.DAC80504(idx=N)` etc. and uses the proxy
methods. There is no dashboard-specific RPC or privileged path.
Every HTTP endpoint translates to one or more broker RPC calls —
except the two CAN trace endpoints, which read SocketCAN directly;
see [`broker-api.md`](broker-api.md) for the method reference.

---

## Read these next

- [`broker-api.md`](broker-api.md) — every RPC the dashboard
  uses under the hood.
- [`operator-guide.md`](operator-guide.md) — day-to-day recipes
  that mix dashboard clicks with CLI.
- [`hardware-reference.md`](hardware-reference.md) — what each
  address / carrier / relay is wired to.
