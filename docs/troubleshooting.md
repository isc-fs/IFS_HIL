# Troubleshooting

Every failure mode we have genuinely seen on this bench, with the
shortest path we know to diagnose and fix it. If something fails and
isn't here, add it — document drift is the enemy.

## Quick diagnostic commands

Bookmark these; you'll run them a lot.

```sh
# Service state
systemctl is-active hil-psu-on hil-can-up hil-broker

# GPIO state
pinctrl get 7 8                    # PS_ON, PWR_OK
pinctrl get 27 17 18               # MCP2515 CS pins

# CAN interfaces
ip -br link | grep can             # UP / DOWN / BUS-OFF
ip -d -s link show can2            # detailed stats + berr-counter

# Kernel messages
sudo dmesg | grep -iE 'mcp251|undervoltage'

# Broker
ls -l /run/hil-broker/broker.sock
journalctl -u hil-broker -n 40 --no-pager

# I²C / SPI
ls /dev/spidev0.* /dev/i2c-1
i2cdetect -y 1                     # apt install i2c-tools
```

---

## By symptom

### `mcp251x spiN: probe with driver mcp251x failed with error -110`

`-ETIMEDOUT` during the probe sequence. Root cause tree:

1. **Running the stock unpatched `mcp251x` module.**
   ```sh
   sudo xz -dc /lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz \
        | strings | grep -i backplane_hil
   ```
   If that prints nothing, you're on the stock module. Run
   [`infra/kernel-module/mcp251x-patched/build.sh`](../infra/kernel-module/mcp251x-patched/)
   and reboot.

2. **PSU not on when the kernel probed.** The chips are on +3V3
   main; if `PS_ON#` wasn't asserted at firmware stage, the probe
   hits a powered-off chip.
   ```sh
   grep -E '^gpio=' /boot/firmware/config.txt
   # Expected:
   #   gpio=7=op,dl
   #   gpio=8=ip,pd
   ```
   Missing? Add them and reboot.

3. **Chip in a bad state from a previous session.** The ATX PSU
   stays on across Pi reboots because firmware re-asserts GPIO7,
   so chip state can persist. Force a real power cycle:
   ```sh
   sudo pinctrl set 7 op dh    # PSU off
   sleep 2
   sudo pinctrl set 7 op dl    # PSU on
   sudo modprobe -r mcp251x
   sudo modprobe mcp251x
   ```

4. **Undervoltage on the Pi.** Check `sudo dmesg | grep -i undervoltage`
   and `vcgencmd get_throttled` (non-zero = problem). Pi 4 needs
   ≥ 3 A on its 5 V input. Common trap: powering the Pi from the
   ATX 5V standby rail with a PSU that only supplies ~2 A there.
   Use a dedicated 5 V / 3 A supply or route through a beefier rail.

### `mcp251x spiN: Cannot initialize MCP2515. Wrong wiring?`

`-ENODEV` at the CANCTRL sanity-check step. Our patches bypass this
check because the CANCTRL register reads unreliably on this board.
If you see this error, the patched module is **not** active.
See the stock-module check above.

### `RTNETLINK answers: Connection timed out` on `ip link set canN up`

The ndo_open path calls the driver's wake-from-sleep, which fails
if the chip isn't actually alive. Same remediation tree as the
-110 probe failure. Often means PSU isn't on.

```sh
pinctrl get 7    # must show "op .. lo"
pinctrl get 8    # must show "ip .. hi"
```

### `can-flasher discover` returns "No bootloaders replied"

Three things in order:

1. **Wrong channel.** The MLC carriers are on PCB CAN1 = kernel
   `can2`. Not `can0`. See
   [`hardware-reference.md`](hardware-reference.md#can-netdev--pcb-label-mapping-crucial).

2. **Carrier not powered.**
   ```python
   c.call('ina.current', addr=0x40) * 1000   # MLC1 → mA
   ```
   Should be ~130 mA for a running bootloader. ≤ 1 mA means the
   relay didn't close or the carrier fuse is blown.

3. **App is already running.** `flash --jump` gave the target to
   the app, which doesn't listen on BL CAN IDs. Send it back to
   the bootloader:
   ```sh
   can-flasher \
     --interface socketcan --channel can2 --bitrate 500000 \
     --node-id 0x1 \
     send-raw 0x001 03 06 01
   # app ACKs, does NVIC_SystemReset, BL holds on next boot
   ```

### `can-flasher discover` shows two rows with the same node ID

Multiple bootloaders on the same bus, both at the factory default
`0x01`, colliding during ISO-TP reassembly. You'll also see
`NoFirstFrame` or `BadSeq` warnings.

- **Quick fix**: power one carrier at a time via the dashboard
  or `tca.write_pin`.
- **Permanent fix**: provision distinct node IDs with
  `can-flasher ... config --set node-id 0xN` per board.

### `flash failed ... No buffer space available (os error 105)` during flash

`ENOBUFS` from socketcan. The canN `txqueuelen` is too small for
sustained flash writes.

```sh
ip -o link show can2 | grep -oE 'qlen [0-9]+'
# Expected: qlen 1000
```

If it's 10, `hil-can-up.service` isn't active. Start it:
```sh
sudo systemctl start hil-can-up
```

Or fix the interface manually for the current session:
```sh
sudo ip link set can2 down
sudo ip link set can2 txqueuelen 1000
sudo ip link set can2 up type can bitrate 500000 restart-ms 200
```

### `flash failed ... session RX task exited — backend may have disconnected: device not found / timeout`

The canN interface went DOWN mid-flash. Causes seen in the wild:

- Momentary bus-off (chip got overwhelmed by errors). With
  `restart-ms=200` the kernel auto-recovers; just retry. The
  flasher's `--verify-after` + idempotent erase-skip-writes mean
  retry is safe.
- Something else on the Pi brought canN down. Check
  `journalctl -u hil-can-up -b`.

### Dashboard shows everything as red / not responding

Broker isn't running, or can't reach hardware.

```sh
systemctl status hil-broker
ls -l /run/hil-broker/broker.sock
# socket present = broker up
```

If the socket's missing:
```sh
journalctl -u hil-broker -b -n 50 --no-pager
```

Common culprits in the log: Python import error (broken `pip
install -e .`), SPI/I²C permission error (user not in `spi`/`i2c`
groups, or `pip install` failed to reach sudo).

### `Address already in use` on port 8080

Another dashboard instance is still running:
```sh
pkill -f dashboard/app.py
```

### `pytest tests/hil/` all skipped

The HIL fixtures skip everything when they can't reach the broker.
Usually because `HIL_BROKER_SOCKET` isn't set to the right path
and the default `/run/hil-broker/broker.sock` isn't there (e.g.
you started the broker manually at `/tmp/hil-broker.sock`):

```sh
export HIL_BROKER_SOCKET=/tmp/hil-broker.sock
pytest tests/hil/
```

Or run under the systemd-managed socket:
```sh
unset HIL_BROKER_SOCKET
pytest tests/hil/
```

### `pytest tests/broker/` fails with `ModuleNotFoundError: broker.fake_bus`

pytest is running from a directory that doesn't have the repo root
on `sys.path`. Run from the repo root:
```sh
cd ~/IFS08_HIL
pytest tests/broker/
```

### `ip link set canN up` succeeds but `candump` shows nothing

Most likely the chip is alive but nothing else on the bus is
transmitting. If you expect traffic, check:

- Is the relay for the transmitting carrier energised?
- Is the far-end ECU actually running (INA226 current > 100 mA)?
- Is the far-end speaking at the same bitrate (500 kbit/s)?

Put the chip in loopback mode and `cansend` + `candump` to prove
the kernel path works:
```sh
sudo ip link set can2 down
sudo ip link set can2 up type can bitrate 500000 loopback on
candump can2 &
cansend can2 123#DEADBEEF
```

Sent frame should come right back on the same interface.

### `undervoltage detected!` in dmesg

The Pi's input voltage dipped below ~4.63 V. SPI signalling gets
unreliable; probes fail, reads corrupt. Get a proper 5 V / 3 A
supply for the Pi (do not rely on the ATX 5VSBY rail unless it's
explicitly rated at 3 A+).

### `BUS-OFF` state sticky

`hil-can-up.service` sets `restart-ms=200`, so bus-off should
self-recover. If an interface is stuck:

```sh
ip -d link show can2 | grep -E 'state|restart-ms'
```

`restart-ms 0` means auto-recovery isn't configured (the service
didn't run). Restart it:
```sh
sudo systemctl start hil-can-up
```

Or fix for the session:
```sh
sudo ip link set can2 down
sudo ip link set can2 up type can bitrate 500000 restart-ms 200
```

If even then it re-enters bus-off immediately, you have a real
bus problem: no peer, bitrate mismatch with the peer, termination
wrong, or a short on the bus wires.

### `sudo -n ip link set ...` fails with `a password is required`

The sudoers drop-in isn't installed or the user isn't `isc`:
```sh
ls -l /etc/sudoers.d/hil-broker    # expected: -r--r----- root:root
sudo visudo -c                     # must say parsed OK
```

If you run the broker under a different username, edit the
sudoers file accordingly.

### `/dev/spidev0.3` does not exist

The `mcp2515-triple` overlay isn't loaded:

```sh
grep dtoverlay /boot/firmware/config.txt
```

Expected: `dtoverlay=mcp2515-triple` (not `spi0-0cs`). If it's
still `spi0-0cs`, edit config.txt and reboot. If it's
`mcp2515-triple` but the device still doesn't exist, the overlay
failed to load — check `sudo dmesg | grep -i 'overlay\|spi0'`.

### `I2C devices: []` in a scan

Either the I²C bus isn't enabled or all chips are off.

```sh
sudo raspi-config nonint do_i2c 0     # enable I²C and reboot
ls /dev/i2c-*                         # expected: /dev/i2c-1
i2cdetect -y 1
```

Note INA226 and TCA9555 are on +3V3SBY, so they answer even with
the PSU off. If I²C enumerates zero chips, the problem is the Pi
side (I²C disabled, cable disconnected, bad pull-ups on a bodged
board), not the PSU.

### Pi SSH drops during CAN traffic

If you're over a VPN, that's your problem — not the Pi. Verified
by `systemctl status` showing uptime unchanged after the drop.
Try a direct LAN connection for high-throughput work.

If the Pi itself did reboot (uptime reset), suspect undervoltage
or a power blip from relay kick-back. Add a larger bulk cap on
the +12V relay rail, or re-verify flyback diodes D1–D4 are
populated.

---

## Logs to gather before asking for help

When opening an Issue or asking in team chat:

```sh
sudo dmesg | tail -60 > /tmp/dmesg.txt
journalctl -u hil-psu-on -u hil-can-up -u hil-broker -b \
    --no-pager > /tmp/services.txt
ip -s -d link show can0 can1 can2 > /tmp/can.txt
pinctrl get 4-12 > /tmp/gpio.txt
systemctl list-unit-files --state=enabled | grep hil- > /tmp/units.txt
vcgencmd get_throttled > /tmp/throttle.txt
```

Attach the `/tmp/*.txt` files or paste the relevant bits inline.
Having all of these up front is usually enough to diagnose
anything you'll hit.
