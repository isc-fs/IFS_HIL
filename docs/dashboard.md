# Dashboard reference

Lightweight Flask web UI for inspecting and controlling the
BACKPLANE_HIL bench. Serves a single-page dark-themed UI and a
JSON API; both read from an in-memory cache the dashboard
maintains by polling the broker every 2 s.

Source: [`dashboard/app.py`](../dashboard/app.py) and
[`dashboard/index.html`](../dashboard/index.html).

---

## Launching the dashboard

Manually:

```sh
pi$ cd ~/IFS08_HIL
pi$ nohup python3 dashboard/app.py > /tmp/dashboard.log 2>&1 &
```

Or with explicit arguments:

```sh
pi$ python3 dashboard/app.py --port 8080 --poll-interval 2.0
```

The dashboard needs a reachable broker socket. Default:
`/run/hil-broker/broker.sock`, overridable via
`HIL_BROKER_SOCKET`. With `hil-broker.service` enabled it's just
there.

A systemd `hil-dashboard.service` is a known follow-up; not
shipped today.

---

## Web UI (`/`)

Layout:

- **Status row** — timestamp of the last successful poll, PSU
  state (on / off), `PWR_OK` indicator, nRF24 presence.
- **PSU control** — toggle that calls `/api/psu/power`.
- **Carriers** — one row per MLC slot (MLC1..MLC4) with:
  - Relay-on toggle (calls `/api/carrier/<slot>/power`).
  - Live INA226 current reading in mA. Overcurrent rows
    highlight red when `|current| > MLC_CURRENT_MAX_A` (3 A
    default).
  - INA226 power reading in mW and shunt voltage in mV.
  - Carrier-present indicator (from INA226 presence check).
- **CAN** — one row per MCP2515 with:
  - Current operating mode (normal / loopback / listen-only / …).
  - Live TEC/REC counters.
  - Mode-change dropdown that calls `/api/can/<idx>/mode`.
  - The row labels show the **PCB labels** (CAN1/CAN2/CAN3).
    Keep the
    [kernel netdev mapping](hardware-reference.md#can-netdev--pcb-label-mapping-crucial)
    handy when cross-referencing with `ip link`.
- **DAC** — one row per DAC80504 with per-channel setpoint inputs
  that post to `/api/dac/<idx>/channel/<ch>`. Shows the last-set
  voltage (read from the shadow register).
- **ADC** — one row per MCP3208, showing all 8 channels' live
  voltages.
- **I/O expanders** — one row per TCA9555 with port0 and port1
  values in hex, per-pin state indicators.

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
`--poll-interval`). Endpoints never block on hardware — they
always return the most recent snapshot.

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

Drive one TCA9555 pin. `addr` is the I²C address (0x20, 0x21, or
0x22), `port` is 0 or 1, `pin` is 0..7.

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

---

## Internal poll loop

`_poll()` runs every `--poll-interval` seconds (default 2.0) on a
background thread. Per cycle:

- One broker call each for PSU, 3× CAN status, 3× ADC `read_all`,
  4× DAC `get_voltage`×4, 4× INA snapshot, 3× TCA read, 1× nRF
  presence. ~20 broker RPCs.
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
Every HTTP endpoint translates to one or more broker RPC calls;
see [`broker-api.md`](broker-api.md) for the method reference.

---

## Read these next

- [`broker-api.md`](broker-api.md) — every RPC the dashboard
  uses under the hood.
- [`operator-guide.md`](operator-guide.md) — day-to-day recipes
  that mix dashboard clicks with CLI.
- [`hardware-reference.md`](hardware-reference.md) — what each
  address / carrier / relay is wired to.
