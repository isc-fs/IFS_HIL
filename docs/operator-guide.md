# Operator guide

Day-to-day recipes for running the bench. Assumes
[`docs/getting-started.md`](getting-started.md) has been completed (or
[`scripts/bench_setup.sh`](../scripts/bench_setup.sh) has run) —
patched kernel module, device-tree overlay, sudoers, and the systemd
units are all installed.

If something doesn't behave as expected here, jump to
[`docs/troubleshooting.md`](troubleshooting.md).

---

## Preflight: is the bench healthy?

Five commands that tell you everything is wired right:

```sh
pi$ systemctl is-active hil-psu-on hil-can-up hil-broker   # active × 3
pi$ ip -br link | grep can                                 # can0/can1/can2 UP
pi$ ls /dev/spidev0.{3..11} /dev/i2c-1                     # 9 spidev nodes + i2c
pi$ pinctrl get 7,8                                        # 7 = op lo, 8 = ip hi
pi$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/status
                                                           # 200
```

If all five pass, skip ahead. If any fail, see
[troubleshooting](troubleshooting.md). (`pinctrl get 7 8`, with a space,
fails with "Too many arguments" on current `pinctrl`.) Missing
`spidev0.4`–`0.11` while `spidev0.3` exists means an old overlay: the
DACs and ADCs are running on the userspace chip-select fallback —
reinstall the overlay and reboot.

For the whole host build in one go, `python3 -m tools.bench doctor`;
for the hardware against the bench's descriptor,
`python3 -m tools.bench verify --bench <id>`.

---

## Starting and stopping services

The bench runs these systemd units in dependency order:

| Unit | Role | Type |
|---|---|---|
| `hil-psu-on.service` | Assert `PS_ON#`, configure `PWR_OK` pin | oneshot |
| `hil-can-up.service` | `ip link set canN up …` for all three chips | oneshot |
| `hil-broker.service` | Broker daemon; single owner of SPI/I²C/GPIO | simple |
| `hil-dashboard.service` | Flask observability UI on `:8080` | simple |
| `hil-bench-watchdog.timer` | Every 5 min: verify, and recover if wedged — see [Self-healing](#self-healing-and-recovery) | timer |

A bench that takes CI runs also runs the GitHub Actions runner service
(`actions.runner.isc-fs-IFS_HIL.<bench>.service`) with a
`Restart=always` drop-in — see
[`infra/systemd/README.md`](../infra/systemd/README.md).

```sh
pi$ # cold start (or after a reboot if something is off)
pi$ sudo systemctl start hil-psu-on hil-can-up hil-broker hil-dashboard hil-bench-watchdog.timer

pi$ # stop everything cleanly — the watchdog timer FIRST: left running,
pi$ # its next tick restarts hil-broker, whose Wants= brings the CAN
pi$ # units and the PSU back with it, within 5 minutes
pi$ sudo systemctl stop hil-bench-watchdog.timer hil-dashboard hil-broker hil-can-up hil-psu-on

pi$ # full bounce — useful after editing broker code
pi$ sudo systemctl restart hil-broker

pi$ # view service logs
pi$ journalctl -u hil-broker -f          # follow live
pi$ journalctl -u hil-dashboard -f       # dashboard, live
pi$ journalctl -u hil-bench-watchdog -f  # watchdog, live
pi$ journalctl -u hil-can-up -b          # this boot only
```

---

## Dashboard access

The dashboard is a systemd service and starts automatically at boot
(see [`infra/systemd/hil-dashboard.service`](../infra/systemd/hil-dashboard.service)).

```sh
pi$ systemctl status hil-dashboard       # check it's running
pi$ sudo systemctl restart hil-dashboard # after editing dashboard code
pi$ journalctl -u hil-dashboard -f       # follow logs
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

If port 8080 won't bind, a stray `nohup` instance from a pre-service
install may still be running:

```sh
pi$ pkill -f dashboard/app.py
pi$ sudo systemctl restart hil-dashboard
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
pi$ sudo systemctl restart hil-can-up   # bitrate, sample point, txqueuelen
```

If the chip itself has latched a bad state (rare), cycle the PSU —
and then restart `hil-can-up` too, because the PSU cycle resets the
MCP2515s while the kernel still shows the links up:

```python
>>> c.call('psu.power', on=False); import time; time.sleep(2)
>>> c.call('psu.power', on=True)
```

```sh
pi$ sudo systemctl restart hil-can-up hil-broker
```

### A wedged `mcp251x`

If `can2` traffic stops while `ip -d link show can2` still says
`UP` / `ERROR-ACTIVE`, the MCP2515 driver has wedged — typically after
`can2` was reconfigured while a carrier was under test, or after a PSU
cycle. It looks exactly like the firmware stopped transmitting, so rule
it out before suspecting the firmware. Reload the module and bring the
links back:

```sh
pi$ sudo modprobe -r mcp251x && sudo modprobe mcp251x
pi$ sudo systemctl restart hil-can-up hil-broker
```

To avoid provoking it, don't reconfigure `can2` (`ip link set can2 …`)
while a carrier is under test.

---

## Self-healing and recovery

`hil-bench-watchdog.timer` runs `python3 -m tools.bench watchdog`
every 5 minutes. It verifies the bench and, only if something is
wrong, climbs a ladder and stops at the first rung that works:

| Level | Action | Takes |
|---|---|---|
| L1 | restart `hil-broker` | ~6 s |
| L2 | PSU power-on reset + reload `mcp251x` + restart `hil-can-up` and `hil-broker` | ~27 s |

It takes the bench lock (`/tmp/hil-bench.lock`) **non-blocking**: if a
flash or a test run holds it, the watchdog skips that cycle rather than
cycling the rails under a flash. Before recovering it appends the
wedged state to `~/hil-wedge-evidence.jsonl`. It logs healthy checks
too — a bench that needs recovering on every cycle is a worsening
fault, visible only against the quiet passes. If the self-hosted runner
isn't active it reports `RUNNER DOWN` and exits non-zero; it never
restarts the runner itself.

Run a rung by hand (it takes the lock itself):

```sh
pi$ python3 -m tools.bench recover --bench bench-01 --level 1
pi$ python3 -m tools.bench recover --bench bench-01 --level 2
```

The case it was built for is the **DAC wedge**: a DAC80504 returns a
wrong device id (expected `0x0417`) and stops following setpoints.
Since the chip-selects moved into the kernel (#124) it should be rare;
an L2 clears any residue.

---

## Running the HIL test suite

This is the by-hand path, on the bench. To test a firmware PR instead,
label it `hil-test` or comment `/hil-test` and let CI build, flash and
report — see
[`docs/development/testing.md`](development/testing.md#running-a-suite-from-a-firmware-pr),
which also lists the named suites so a developer picks what runs.

`tests/hil/` holds two kinds of test:

- **Bench self-tests** — the top-level `tests/hil/test_*.py` (CAN, SPI
  DAC/ADC, I²C, relays, MLC power). They exercise the bench through
  the broker and flash nothing.
- **DUT suites** — `tests/hil/vcu/` (the ECU; the directory name is
  historical) and `tests/hil/ams/`. They drive a carrier, and Block A
  **reflashes** it.

```sh
pi$ cd ~/IFS_HIL
pi$ pytest tests/hil/ --ignore=tests/hil/vcu --ignore=tests/hil/ams -v   # self-tests
pi$ pytest tests/hil/test_can.py -v
pi$ pytest tests/hil/test_spi_dac.py -v -k test_channel_sweep
```

To run a DUT suite by hand, expand a named suite from
[`configs/suites.yaml`](../configs/suites.yaml) exactly as CI does,
and take the bench lock so a dispatched run can't land mid-session:

```sh
pi$ export ECU_FIRMWARE_BIN=/path/to/ECU08.bin   # the image under test
pi$ flock /tmp/hil-bench.lock \
      pytest $(python3 -m tools.bench suite --dut ecu --suite smoke) -v
```

Set `ECU_FIRMWARE_BIN` / `AMS_FIRMWARE_BIN` first: A-003 reflashes the
carrier from it. With `ECU_FIRMWARE_BIN` unset, a hand run falls back
to `~/firmware-builds/ECU_fix.bin` — a stale 2026-06 diagnostic build
that exists on bench-01 — and every later case then judges that image
instead of yours.

Tests auto-skip cleanly if the broker socket is unreachable, so you
won't see confusing failures when the bench is off.

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

### The wrapper: `tools/flash_dut.py`

This is what CI runs, and the easiest way to flash by hand:

```sh
pi$ flock /tmp/hil-bench.lock \
      python3 -m tools.flash_dut --dut ecu --bin /path/to/ECU08.bin   # or --dut ams
pi$ python3 -m tools.flash_dut --dut ams --bin /path/to/AMS.bin --dry-run   # plan only
```

**Flash under the bench lock**, as CI does — and wrap a raw
`can-flasher` flash the same way. Neither takes the lock itself. The
watchdog skips a bench whose lock is held; otherwise it runs its
checks, and if one fails mid-flash its level-2 recovery cycles the PSU.
Power lost mid-flash can leave an STM32H7 unrecoverable.

It reads the carrier slot and relay from the bench descriptor, and the
node id, app address and boot trigger from the DUT profile; powers
**only** the target carrier (every other DUT slot is de-energised);
waits for the app to start talking, then sends its boot trigger; checks
that exactly one bootloader answers and that its product string is the
expected one (`IFS08-CE-ECU` / `IFS08-CE-AMS`); and refuses to start with
a `can-flasher` older than 2.8.0. The rest of this section is the raw
`can-flasher` path underneath it.

### Checklist before first flash

- [ ] `can-flasher --version` prints `2.8.0` or later. Older builds have
      no ISO-TP session recovery: they erase, then fail mid-image and
      leave the carrier with no app.
- [ ] `can-flasher adapters` lists `can0`, `can1`, `can2`.
- [ ] Carrier you're targeting is powered (INA226 ~ 130 mA, not 0).
- [ ] Target ECU's bootloader is burned. The HIL bench does **not**
      write the bootloader — that's a one-time SWD step, done
      elsewhere.
- [ ] You know the target node ID: ECU `0x01`, AMS `0x02`, uDV `0x03`.
      The id lives in the carrier's bootloader NVM, so confirm it with
      `discover`; see "Multi-board flashing" below.

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
- `--node-id 0x1` — the ECU's bootloader node ID (the AMS is `0x2`).
  It must be the id the carrier actually answers on; if `discover`
  lists the node but `flash` fails with `CONNECT failed: timed out`,
  this is why.
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

Even current `can-flasher` builds occasionally NACK `BAD_SESSION`
part-way through an image. The bootloader stays reachable, so retry
the flash before suspecting the firmware or the board.

### Multi-board flashing

Node ids are provisioned per carrier — ECU `0x01`, AMS `0x02`, uDV
`0x03` — but a carrier can drift from the scheme: bench-01's AMS
answered `0x01` until it was re-provisioned on 2026-09-25. If two
powered carriers answer on the same id, both respond to the discover
broadcast and you see ISO-TP reassembler warnings plus two rows in the
output with the same node ID.

Two options:

**Option A (simplest): power one carrier at a time** — which is what
`flash_dut` always does. Via the dashboard toggles or by flipping
TCA9555 pins directly:

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
jumped away. That's expected after a `flash --jump`. To send the app
back to the bootloader without touching the board, send the DUT's boot
trigger — `bl_trigger_id` / `bl_trigger_payload` in its profile:

```sh
pi$ cansend can2 002#B007AD12    # ECU (tests/hil/vcu/vcu_profile.yaml)
pi$ cansend can2 002#B007AD11    # AMS (tests/hil/ams/ams_profile.yaml)
```

The app reboots into its bootloader, which then answers `discover`.
(The demo firmware uses a different trigger, `can-flasher … send-raw
0x001 03 06 01`: ISO-TP PCI `0x03` = 3-byte single frame, `0x06` =
`APP_CTRL` message, `0x01` = `ENTER_BOOTLOADER` opcode. See the demo
firmware's README for that protocol.)

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
pi$ journalctl -u hil-dashboard -f           # dashboard, live
pi$ journalctl -u hil-bench-watchdog -f      # watchdog: checks, recoveries, RUNNER DOWN
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
pi$ sudo systemctl stop hil-bench-watchdog.timer hil-broker hil-can-up
pi$ sudo systemctl stop hil-psu-on      # de-asserts PS_ON# via ExecStop
pi$ sudo poweroff                       # clean Pi shutdown
```

The `hil-psu-on.service` unit's `ExecStop` directive flips GPIO7
back high, so the ATX rails go down cleanly before the Pi halts.

---

## Where to go next

- **Taking the project over** → [`HANDOVER.md`](../HANDOVER.md).
- **A command above misbehaves** → [`troubleshooting.md`](troubleshooting.md).
- **Need the exact semantics of a broker RPC** →
  [`broker-api.md`](broker-api.md).
- **Want the dashboard's HTTP API** → [`dashboard.md`](dashboard.md).
- **Writing new tests or editing the broker** →
  [`development/testing.md`](development/testing.md) and
  [`development/setup.md`](development/setup.md).
