# Operator guide

Day-to-day recipes for running the bench. Assumes
[`docs/getting-started.md`](getting-started.md) has been completed —
patched kernel module, device-tree overlay, sudoers, and the three
systemd units are all installed.

If something doesn't behave as expected here, jump to
[`docs/troubleshooting.md`](troubleshooting.md).

---

## Preflight: is the bench healthy?

Five commands that tell you everything is wired right:

```sh
pi$ systemctl is-active hil-psu-on hil-can-up hil-broker   # active × 3
pi$ ip -br link | grep can                                 # can0/can1/can2 UP
pi$ ls /dev/spidev0.3 /dev/i2c-1                           # both exist
pi$ pinctrl get 7 8 | head                                 # 7 = op lo, 8 = ip hi
pi$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/status
                                                           # 200
```

If all five pass, skip ahead. If any fail, see
[troubleshooting](troubleshooting.md).

---

## Starting and stopping services

The bench runs three systemd services in dependency order:

| Service | Role | Type |
|---|---|---|
| `hil-psu-on.service` | Assert `PS_ON#`, configure `PWR_OK` pin | oneshot |
| `hil-can-up.service` | `ip link set canN up …` for all three chips | oneshot |
| `hil-broker.service` | Broker daemon; single owner of SPI/I²C/GPIO | simple |

```sh
pi$ # cold start (or after a reboot if something is off)
pi$ sudo systemctl start hil-psu-on hil-can-up hil-broker

pi$ # stop everything cleanly
pi$ sudo systemctl stop hil-broker hil-can-up hil-psu-on

pi$ # full bounce — useful after editing broker code
pi$ sudo systemctl restart hil-broker

pi$ # view service logs
pi$ journalctl -u hil-broker -f     # follow live
pi$ journalctl -u hil-can-up -b     # this boot only
```

> **Note**: The dashboard is currently *not* under systemd; it lives
> in a `nohup` invocation (see [dashboard section](#dashboard-access)
> below). A `hil-dashboard.service` is a known follow-up.

---

## Dashboard access

Start the dashboard (if it's not already running):

```sh
pi$ cd ~/IFS08_HIL
pi$ nohup python3 dashboard/app.py > /tmp/dashboard.log 2>&1 &
pi$ tail /tmp/dashboard.log         # confirm startup
```

Browse to `http://<pi-ip>:8080/`. You get, at a glance:

- PSU state and PWR_OK indicator, with a toggle for `PS_ON#`.
- Per-carrier relay toggles (K1 → MLC1 … K4 → MLC4) with live
  INA226 current readings and an overcurrent highlight.
- MCP3208 ADC channels — 24 total, live.
- DAC80504 output voltages with setpoint inputs.
- MCP2515 CAN mode selector (Normal / Loopback / Listen-only /
  Config) and live TEC / REC counters.
- TCA9555 per-pin state and direction.
- Timestamp of the last successful poll.

To stop the dashboard:

```sh
pi$ pkill -f dashboard/app.py
```

See [`docs/dashboard.md`](dashboard.md) for the HTTP API reference.

---

## PSU and carrier power

### PSU (+12 V / +5 V / +3.3 V main rails)

Via the broker from any Python shell:

```sh
pi$ python3
>>> from broker.server import BrokerClient
>>> c = BrokerClient('/run/hil-broker/broker.sock')
>>> c.call('psu.status')          # {'ps_on': True, 'pwr_ok': True}
>>> c.call('psu.power', on=True)  # turn PSU on, wait for PWR_OK
>>> c.call('psu.power', on=False) # turn PSU off
```

Or from the dashboard's PSU toggle.

> **Note**: `hil-psu-on.service` asserts `PS_ON#` at boot, so on a
> healthy bench the PSU is on before the broker ever starts.
> `psu.power(False)` is mainly useful for planned power cycling.

### Carrier relays (K1–K4 → MLC1–MLC4)

The +12 V rail behind each relay feeds one MLC carrier slot. When
the relay closes, the carrier gets power and the MCP2515 CAN
transceiver it's wired to becomes reachable.

From the dashboard: toggle the "Carrier N power" switch in the
Power row.

From a shell (example energising K1):

```python
>>> c.call('tca.set_direction', addr=0x20, port=0, mask=0x00)
>>> c.call('tca.write_pin', addr=0x20, port=0, pin=0, value=True)   # K1 on
>>> c.call('ina.current', addr=0x40) * 1000   # MLC1 current in mA
134.77
```

Check current: **~130 mA** = STM32 running bootloader or app.
**≤ 1 mA** = the relay didn't close or the carrier fuse is blown.

---

## Reading sensors

### ADC channels (MCP3208)

```python
>>> c.call('adc.read', idx=0, channel=3)        # raw 12-bit value
2487
>>> c.call('adc.read_voltage', idx=0, channel=3) # float V (VREF = 3.3 V)
2.001
>>> c.call('adc.read_all', idx=1)                # all 8 channels, raw
[3, 7, 12, 4, 1, 0, 2, 1]
```

ADC idx `0`/`1`/`2` map to U9/U10/U11 (PCB labels ADC1/ADC2/ADC3).

### Carrier current (INA226)

```python
>>> s = c.call('ina.read', addr=0x40)            # full snapshot
>>> s
{'bus_voltage_V': 0.005, 'shunt_voltage_V': 0.0013, 'current_A': 0.135, 'power_W': 0.000}
```

Use `0x40` / `0x41` / `0x44` / `0x45` for MLC1 / MLC2 / MLC3 / MLC4.
`bus_voltage_V` is always near 0 because the sensing is low-side —
rely on `current_A` (or `shunt_voltage_V` × 100 for mA).

### TCA9555 I/O expander

```python
>>> c.call('tca.read', addr=0x20)
{'input_port0': 0, 'input_port1': 0,
 'output_port0': 5, 'output_port1': 0,
 'config_port0': 0, 'config_port1': 255}
>>> c.call('tca.read_port', addr=0x20, port=1)   # just one port
0
```

---

## Setting outputs

### DAC channels (DAC80504)

```python
>>> c.call('dac.set_voltage', idx=0, channel=2, volts=1.5)
>>> c.call('dac.get_voltage', idx=0, channel=2)
1.4992...
```

DAC idx `0..3` = U12/U13/U14/U15. 4 channels each. Range 0 to VREF
(3.3 V); the driver clips outside that.

> **Note**: `dac.get_voltage()` reads the chip's shadow input
> register, **not** the analog output. Verify outputs with a meter
> or an ADC channel if debugging hardware issues.

### TCA9555 pins

```python
>>> c.call('tca.set_direction', addr=0x20, port=0, mask=0x00)  # port0 all outputs
>>> c.call('tca.write_pin', addr=0x20, port=0, pin=5, value=True)
>>> c.call('tca.write_port', addr=0x20, port=0, value=0xAA)
```

See [`docs/hardware-reference.md`](hardware-reference.md) for which
TCA9555 pin drives which bench signal.

---

## CAN operations

### Quick bus probe

```sh
pi$ candump -t d can2                  # dump incoming frames (Ctrl-C to stop)
pi$ cansend can2 123#DEADBEEFCAFEBABE  # send one 8-byte frame, ID 0x123
pi$ cangen can2 -n 100 -g 5            # send 100 random frames, 5 ms apart
pi$ ip -s -d link show can2            # detailed stats + berr-counter
```

### Broker CAN methods

The broker exposes the same CAN RPC surface it did before the
kernel-driver migration, now backed by socketcan:

```python
>>> c.call('can.status', idx=2)
{'mode': 0, 'tec': 0, 'rec': 0}
>>> c.call('can.set_mode', idx=2, mode=0x40)       # 0x40 = LOOPBACK
True
>>> c.call('can.loopback_test', idx=2, can_id=0x123,
...        data_b64='3q2+7w==')                    # b'\xde\xad\xbe\xef'
True
>>> c.call('can.read_error_counters', idx=2)
[0, 0]
```

Mode encoding for backwards compatibility:
- `0x80` → kernel link DOWN (= MCP2515 CONFIG)
- `0x00` → link UP, not loopback (= NORMAL)
- `0x40` → link UP with `loopback on`

### Recover from bus-off

Bus-off on a real CAN bus typically means no peer was ACKing
(idle or broken bus). Symptoms: `ip -d link show canN` shows
`state BUS-OFF`, TEC pegged at 256, further `cansend` fails.

`hil-can-up.service` configures `restart-ms=200`, so the kernel
automatically rebrings the interface up after 200 ms. If it's
still stuck:

```sh
pi$ sudo ip link set can2 down
pi$ sudo ip link set can2 up type can bitrate 500000 restart-ms 200
```

If the chip itself has latched a bad state (rare), cycle the PSU:

```python
>>> c.call('psu.power', on=False); import time; time.sleep(2)
>>> c.call('psu.power', on=True)
```

---

## Running the HIL test suite

The bench ships a pytest suite at `tests/hil/` that exercises the
hardware through the broker. With the bench running:

```sh
pi$ cd ~/IFS08_HIL
pi$ pytest tests/hil/ -v
```

Expected: ~93 passed, ~11 skipped (the skips are for unpopulated
hardware like nRF24). Tests auto-skip cleanly if the broker socket
is unreachable, so you won't see confusing failures when the bench
is off.

Run a single test module:

```sh
pi$ pytest tests/hil/test_can.py -v
pi$ pytest tests/hil/test_spi_dac.py -v -k test_channel_sweep
```

The suite runs **concurrently with the dashboard** — the broker
serialises SPI/I²C access across processes, so there's no
contention to worry about. (This was explicitly not the case before
the Phase 3 broker migration; any references in old docs to
"stop the dashboard before running tests" are stale.)

Broker-only unit tests (no hardware needed, uses the fake backend):

```sh
pi$ pytest tests/broker/ -v
```

---

## Flashing an ECU

### Checklist before first flash

- [ ] `can-flasher --version` prints `1.1.2` or later.
- [ ] `can-flasher adapters` lists `can0`, `can1`, `can2`.
- [ ] Carrier you're targeting is powered (INA226 ~ 130 mA, not 0).
- [ ] Target ECU's bootloader is burned. The HIL bench does **not**
      write the bootloader — that's a one-time SWD step, done
      elsewhere.
- [ ] You know the target node ID. Factory-default is `0x01`; see
      "Multi-board flashing" below if you have several ECUs on
      the same bus.

### Single-board flash

The canonical single-ECU flash command:

```sh
pi$ can-flasher \
      --interface socketcan --channel can2 --bitrate 500000 \
      --node-id 0x1 --timeout 10000 \
      flash /path/to/firmware.bin \
      --address 0x08020000 --verify-after --jump
```

- `--channel can2` — PCB CAN1, where the MLC carriers live.
- `--node-id 0x1` — factory-default bootloader node ID. Change if
  you provisioned a custom one (see next section).
- `--address 0x08020000` — default app-image start address for the
  STM32H733 + `isc-fs/stm32-can-bootloader` combo. Flat `.bin`
  files need this explicitly; `.elf` files carry their own.
- `--verify-after` — CRC-checks flash against the image after
  writing.
- `--jump` — on verify pass, boot directly into the app. Without
  this the bootloader holds and waits for another BL command.

Success looks like:

```
Sector 1: queued for rewrite
Sector 1: erased
Sector 1:   0% …  100% (131072/131072 B)
Committing metadata…
Done — erased 1 written 1 skipped 0 in 3421 ms
Flashed /path/to/firmware.bin (crc=0x…, size=… B, …).
  jumped to app at 0x08020000.
```

### Multi-board flashing

The default bootloader node ID is `0x01`. If two carriers are
powered simultaneously, their bootloaders both respond to the
discover broadcast and you see ISO-TP reassembler warnings plus
two rows in the output with the same node ID.

Two options:

**Option A (simplest): power one carrier at a time.** Via the
dashboard toggles or by flipping TCA9555 pins directly:

```python
>>> c.call('tca.set_direction', addr=0x20, port=0, mask=0x00)
>>> # only K1 on:
>>> c.call('tca.write_port', addr=0x20, port=0, value=0x01)
>>> # only K3 on:
>>> c.call('tca.write_port', addr=0x20, port=0, value=0x04)
```

Flash the one that's powered, then switch.

**Option B: provision distinct node IDs.** Each bootloader has a
writable NVM cell for its own node ID. One-time per board:

```sh
# connect only one target at a time
pi$ can-flasher \
      --interface socketcan --channel can2 --bitrate 500000 \
      --node-id 0x1 \
      config --set node-id 0x3    # new node ID
```

After that the board is reachable at `--node-id 0x3`, and both
boards can live on the bus simultaneously without colliding.

### Discover

Probes every bootloader currently listening:

```sh
pi$ can-flasher discover -i socketcan -c can2 --timeout-ms 3000
Node  Proto  FW Version        Git Hash  Product  WRP  Reset Cause
────  ─────  ────────────────  ────────  ───────  ───  ───────────
0x01  0.1    no app installed  —         —        ✗    PIN
```

Returning "no bootloaders replied" with a carrier clearly powered
usually means the app is already running and the bootloader has
jumped away. That's expected after a `flash --jump`. To send the
app back to the bootloader without touching the board:

```sh
pi$ can-flasher \
      --interface socketcan --channel can2 --bitrate 500000 \
      --node-id 0x1 \
      send-raw 0x001 03 06 01
# app ACKs on 0x011, issues NVIC_SystemReset, BL holds on next boot.
```

(The `03 06 01` payload is: ISO-TP PCI `0x03` = 3-byte single
frame, `0x06` = `APP_CTRL` message, `0x01` = `ENTER_BOOTLOADER`
opcode. See the demo firmware's README for the protocol.)

### Post-flash sanity checks

- `can-flasher discover ...` returns **empty** → app is running,
  bootloader not listening. Expected.
- INA226 current drops slightly (or changes noticeably if the new
  app runs differently from the bootloader) → confirms the code
  really jumped.
- Any LEDs on the target board show the app's expected pattern.

---

## Viewing logs

```sh
pi$ journalctl -u hil-broker -f              # broker, live
pi$ journalctl -u hil-broker -b --no-pager   # broker, this boot
pi$ tail -f /tmp/dashboard.log               # dashboard (if nohup'd)
pi$ sudo dmesg -w                            # kernel — watch for mcp251x
pi$ sudo dmesg | grep mcp251x                # just mcp251x history
pi$ sudo dmesg | grep -i undervoltage        # Pi power-quality events
```

When something goes wrong mid-flash, the most useful triad is
`journalctl -u hil-broker -f`, `sudo dmesg -w`, and the flasher's
own stderr.

---

## Safe shutdown

The ATX PSU turns off automatically when the Pi loses power,
because `PS_ON#` (GPIO7) floats on Pi shutdown. If you want to
power the bench down explicitly first:

```sh
pi$ sudo systemctl stop hil-broker hil-can-up
pi$ sudo systemctl stop hil-psu-on      # de-asserts PS_ON# via ExecStop
pi$ sudo poweroff                       # clean Pi shutdown
```

The `hil-psu-on.service` unit's `ExecStop` directive flips GPIO7
back high, so the ATX rails go down cleanly before the Pi halts.

---

## Where to go next

- **A command above misbehaves** → [`troubleshooting.md`](troubleshooting.md).
- **Need the exact semantics of a broker RPC** →
  [`broker-api.md`](broker-api.md).
- **Want the dashboard's HTTP API** → [`dashboard.md`](dashboard.md).
- **Writing new tests or editing the broker** →
  [`development/testing.md`](development/testing.md) and
  [`development/setup.md`](development/setup.md).
