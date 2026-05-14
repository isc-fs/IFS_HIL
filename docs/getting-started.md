# Getting started — from a blank Raspberry Pi to flashing an ECU

This is the reproducible path from a freshly-imaged Raspberry Pi 4 to
a working HIL bench that can discover and flash a bootloader-equipped
STM32 over CAN. Every step is a prerequisite for the next; skipping
ahead is an easy way to burn an afternoon debugging.

Plan on **45 minutes** end-to-end if nothing goes sideways, plus one
reboot.

---

## 0. Prerequisites

### Hardware

- **Raspberry Pi 4 Model B**, 2 GB or more, with a working Ethernet or
  Wi-Fi connection and SSH enabled.
- **BACKPLANE_HIL PCB**, populated per the design review
  (`docs/BACKPLANE_HIL/design_review.md`, not tracked in the repo —
  ask the team for the KiCad project).
- **ATX power supply** with **≥ 3 A on +5 V standby**. Lower-rated
  supplies cause Pi undervoltage events that break SPI signalling.
  A consumer-grade desktop PSU usually has 2 A SBY; server-grade or
  a dedicated 5 V / 3 A brick works.
- **An STM32H733ZG carrier board** flashed with the
  [isc-fs/stm32-can-bootloader](https://github.com/isc-fs/stm32-can-bootloader)
  image. This is assumed to be burned via SWD out-of-band; the HIL
  bench does not burn the bootloader itself.
- **Ethernet cable** or a reachable Wi-Fi SSID; the bench doesn't do
  headless provisioning from nothing.

### Software

- **Raspberry Pi OS Lite (64-bit)**, Bookworm or newer. This guide was
  validated against `6.12.47+rpt-rpi-v8`.
- An SSH client on your workstation.
- A GitHub account with access to
  [`isc-fs/can-flasher`](https://github.com/isc-fs/can-flasher)
  (private repo — you'll need `gh auth login` on the downloading host
  at install time).

### Conventions

Commands prefixed with `$` run on your workstation. Commands prefixed
with `pi$` run on the Pi over SSH. Commands without a prefix can run
anywhere that context is obvious from the surrounding prose.

The default Pi user in this guide is `isc`. If yours differs, adjust
the sudoers file and systemd units accordingly.

---

## 1. Pi OS configuration

Enable hardware interfaces:

```sh
pi$ sudo raspi-config nonint do_spi 0
pi$ sudo raspi-config nonint do_i2c 0
```

Add your user to the hardware groups. `spi`, `i2c`, and `gpio` are
required for direct device access; `dialout` is needed if you ever
plug in a CANable or ST-Link over USB.

```sh
pi$ sudo usermod -aG spi,i2c,gpio,dialout,netdev isc
```

Log out and back in for the groups to take effect.

---

## 2. Install system packages

```sh
pi$ sudo apt-get update
pi$ sudo apt-get install -y \
      python3-can \
      can-utils \
      device-tree-compiler \
      xz-utils \
      libudev-dev \
      pkg-config \
      linux-headers-$(uname -r) \
      git curl
```

- `python3-can` — SocketCAN backend for the broker.
- `can-utils` — `cansend`, `candump`, `cangen` for manual bus work.
- `device-tree-compiler` — needed once to compile our `.dts` overlay.
- `xz-utils` + `linux-headers-$(uname -r)` — needed to build the
  patched `mcp251x` kernel module.

---

## 3. Clone the repo

```sh
pi$ git clone https://github.com/isc-fs/IFS08_HIL.git
pi$ cd IFS08_HIL
pi$ git checkout dev
```

Install Python dependencies in editable mode so the `tools.*` and
`broker.*` packages resolve from your working copy:

```sh
pi$ pip install -e . --break-system-packages
```

(The `--break-system-packages` flag is Pi OS Bookworm's opt-in for
system-wide `pip install`. If you prefer a venv, create one in
`~/IFS08_HIL/.venv`, activate it, and drop the flag.)

---

## 4. Device-tree overlay

The BACKPLANE_HIL wires three MCP2515 CAN controllers onto a shared
SPI0 bus with chip-selects on GPIO27 (CAN1), GPIO17 (CAN2), GPIO18
(CAN3), and interrupts on GPIO4/5/6. No stock Raspberry Pi overlay
covers this; we ship a custom one.

Compile and install the overlay:

```sh
pi$ cd ~/IFS08_HIL
pi$ dtc -@ -I dts -O dtb \
       -o infra/devicetree/mcp2515-triple.dtbo \
          infra/devicetree/mcp2515-triple.dts
pi$ sudo cp infra/devicetree/mcp2515-triple.dtbo \
            /boot/firmware/overlays/
```

Edit `/boot/firmware/config.txt`:

```sh
pi$ sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.pre-hil
pi$ sudo nano /boot/firmware/config.txt
```

Replace the line `dtoverlay=spi0-0cs` with the block:

```
dtoverlay=mcp2515-triple
gpio=7=op,dl
gpio=8=ip,pd
```

Why each entry:

- `dtoverlay=mcp2515-triple` — wires the three MCP2515s and exposes a
  spare `/dev/spidev0.3` the Python register-level driver uses for the
  non-CAN chips (DACs, ADCs, nRF24).
- `gpio=7=op,dl` — asserts `PS_ON#` LOW at firmware stage so the ATX
  main rails are stable before the kernel probes the CAN chips.
- `gpio=8=ip,pd` — forces `PWR_OK` back to pulled-down input so it
  reads correctly (overrides the default SPI0_CE0 pinmux).

Keep `/boot/firmware/config.txt.pre-hil` as your rollback image in
case the next reboot doesn't come up.

---

## 5. Patched `mcp251x` kernel module

The stock `mcp251x` driver does not probe on this hardware because of
three hardware-level quirks (see
[`docs/design/mcp251x-driver-patches.md`](design/mcp251x-driver-patches.md)
for why). Our out-of-tree build fixes them.

```sh
pi$ cd ~/IFS08_HIL/infra/kernel-module/mcp251x-patched
pi$ ./build.sh
```

The script fetches the matching upstream source, applies our patch,
builds against the running kernel's headers, compresses the module,
and installs it at
`/lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz`.
The stock module is preserved at `mcp251x.ko.xz.orig` for rollback.

Verify the build artifact carries our markers:

```sh
pi$ sudo xz -dc /lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz \
       | strings | grep -i backplane_hil
# Expected: several "/* Patched: … */" markers
```

---

## 6. Install the sudoers drop-in

The broker runs as the `isc` user. Managing `canN` link state
(`ip link set can0 up …`) needs `CAP_NET_ADMIN`. We grant the narrow
escalation via `sudo -n`:

```sh
pi$ cd ~/IFS08_HIL
pi$ sudo cp infra/sudoers.d/hil-broker /etc/sudoers.d/hil-broker
pi$ sudo chmod 0440 /etc/sudoers.d/hil-broker
pi$ sudo visudo -c    # parse-check; "parsed OK"
```

Only `ip link set canN …` is allowed; no other escalation is granted.

---

## 7. Install systemd units

Four units manage the bench at boot, in this order:

1. `hil-psu-on.service` — asserts `PS_ON#` in userspace (complements
   the firmware `gpio=7` directive; compensates for the Pi 4's GPIO
   output-state persistence across reboots).
2. `hil-can-up.service` — brings `can0`, `can1`, `can2` up at
   500 kbps with `txqueuelen=1000` and `restart-ms=200`.
3. `hil-broker.service` — starts the broker daemon; depends on both.
4. `hil-dashboard.service` — Flask UI on `:8080`; broker client.

Install all four:

```sh
pi$ cd ~/IFS08_HIL/infra/systemd
pi$ sudo cp hil-psu-on.service hil-can-up.service \
            hil-broker.service hil-dashboard.service \
            /etc/systemd/system/
pi$ sudo systemctl daemon-reload
pi$ sudo systemctl enable hil-psu-on.service \
                          hil-can-up.service \
                          hil-broker.service \
                          hil-dashboard.service
```

Do **not** `systemctl start` them yet — they need the patched kernel
module and overlay active, which only happens after reboot.

---

## 8. Reboot and verify

```sh
pi$ sudo reboot
```

Wait about 30 seconds, reconnect, and run through this checklist:

```sh
pi$ # kernel driver bound to all three chips
pi$ sudo dmesg | grep mcp251x
# Expected:
#   mcp251x: loading out-of-tree module taints kernel.
#   mcp251x spi0.2 can0: MCP2515 successfully initialized.
#   mcp251x spi0.1 can1: MCP2515 successfully initialized.
#   mcp251x spi0.0 can2: MCP2515 successfully initialized.

pi$ # canN interfaces up at 500 kbps
pi$ ip -br link | grep can
# Expected: can0 UP, can1 UP, can2 UP

pi$ # spidev0.3 exists for the non-CAN chips
pi$ ls /dev/spidev0.3
# Expected: /dev/spidev0.3

pi$ # PSU_ON (GPIO7) driven LOW, PWR_OK (GPIO8) reads HIGH
pi$ pinctrl get 7 8 | head
# Expected:   7: op -- .. lo
#             8: ip    pd | hi

pi$ # broker and its socket up
pi$ systemctl status hil-broker --no-pager | head
pi$ ls -l /run/hil-broker/broker.sock
# Expected: socket present, broker "active (running)"

pi$ # dashboard service up and listening on 8080
pi$ systemctl status hil-dashboard --no-pager | head
pi$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/status
# Expected: dashboard "active (running)", HTTP 200
```

If any of these fails, go to
[`docs/troubleshooting.md`](troubleshooting.md) before proceeding.

---

## 9. Reach the dashboard

The dashboard is already running as `hil-dashboard.service` (enabled
in step 7, started on the reboot in step 8). Point a browser at
`http://<pi-ip>:8080/` — you should see PSU state, carrier power
monitors, ADC/DAC channels, CAN mode indicators, and TCA9555 I/O
state, all updating.

If port 8080 is unreachable:

```sh
pi$ systemctl status hil-dashboard      # is it running?
pi$ journalctl -u hil-dashboard -b -n 50 --no-pager
pi$ pkill -f 'dashboard/app.py'         # kill any stray nohup
pi$ sudo systemctl restart hil-dashboard
```

---

## 10. Install `can-flasher`

`can-flasher` is the host-side tool that speaks the STM32 bootloader
protocol. It's a Rust binary published as a private release.

On a workstation with `gh` authenticated:

```sh
$ gh release download v1.1.2 -R isc-fs/can-flasher \
       -p 'can-flasher-v1.1.2-aarch64-unknown-linux-gnu.tar.gz'
$ scp can-flasher-v1.1.2-aarch64-unknown-linux-gnu.tar.gz isc@<pi-ip>:/tmp/
```

On the Pi:

```sh
pi$ cd /tmp
pi$ tar -xzf can-flasher-v1.1.2-aarch64-unknown-linux-gnu.tar.gz
pi$ sudo install -m 0755 \
       can-flasher-v1.1.2-aarch64-unknown-linux-gnu/can-flasher \
       /usr/local/bin/
pi$ can-flasher --version     # expect: can-flasher 1.1.2
pi$ can-flasher adapters      # expect: SocketCAN interfaces: can0 can1 can2
```

---

## 11. First discovery

**Important** gotcha: the kernel's `mcp251x` probes SPI children in
reverse order, so the kernel `canN` names are **inverted** relative
to the PCB labels:

| kernel netdev | PCB label | MLC carrier ECUs live here |
|---|---|---|
| `can0` | CAN3 (U21) | — |
| `can1` | CAN2 (U19) | — |
| `can2` | **CAN1 (U17)** | ✅ yes |

The MLC1..MLC4 carriers are all wired to PCB CAN1, which is kernel
`can2`. Any flash command targets `can2`.

Put your carrier (say MLC1) under power — via the dashboard's
"Carrier 1 power" toggle or from the shell:

```sh
pi$ export HIL_BROKER_SOCKET=/run/hil-broker/broker.sock
pi$ python3 -c "
from broker.server import BrokerClient
c = BrokerClient('${HIL_BROKER_SOCKET}')
c.call('tca.set_direction', addr=0x20, port=0, mask=0x00)
c.call('tca.write_pin', addr=0x20, port=0, pin=0, value=True)  # K1 on
print('MLC1 current:', c.call('ina.current', addr=0x40) * 1000, 'mA')
"
```

A running STM32 bootloader draws about **130 mA**. If you see ≤ 1 mA,
the carrier isn't powered — check the relay and fuse before
proceeding. See [`docs/troubleshooting.md`](troubleshooting.md).

Run discovery:

```sh
pi$ can-flasher discover -i socketcan -c can2 --timeout-ms 3000
```

Expected output:

```
Node  Proto  FW Version        Git Hash  Product  WRP  Reset Cause
────  ─────  ────────────────  ────────  ───────  ───  ───────────
0x01  0.1    no app installed  —         —        ✗    PIN
```

If multiple carriers have power and each bootloader reports node
`0x01` (factory default), you'll see collisions in the ISO-TP
reassembler output. Power one carrier at a time for first runs, or
provision distinct node IDs via `can-flasher config`.

---

## 12. First flash

Use any `.bin` for the STM32H733ZG. The
[`can-flasher`](https://github.com/isc-fs/can-flasher) repo ships a
trivial demo at `demo/MAIN_IFS08_DEMO.bin` — copy it to the Pi:

```sh
$ scp /path/to/MAIN_IFS08_DEMO.bin isc@<pi-ip>:/tmp/
```

Flash and jump:

```sh
pi$ can-flasher \
      --interface socketcan --channel can2 --bitrate 500000 \
      --node-id 0x1 --timeout 10000 \
      flash /tmp/MAIN_IFS08_DEMO.bin \
      --address 0x08020000 --verify-after --jump
```

Expected tail:

```
Committing metadata…
Done — erased 1 written 1 skipped 0 in …
Flashed … crc=0x…, size=26172 B …
jumped to app at 0x08020000.
```

Running `can-flasher discover -i socketcan -c can2` after the jump
should return **no** bootloaders — the app has control and is not
listening on the BL CAN IDs. That's success.

---

## 13. Verification checklist

At this point you have:

- [x] Kernel `mcp251x` driver binding all three MCP2515s.
- [x] `can0`/`can1`/`can2` up at 500 kbps, `txqueuelen=1000`.
- [x] `hil-broker` running as a systemd service with the socket at
      `/run/hil-broker/broker.sock`.
- [x] Dashboard serving at `http://<pi-ip>:8080/`.
- [x] `can-flasher` installed and able to discover + flash an ECU.

You're done. Anything else — regression tests, multi-ECU flashing,
CI wiring — is the operator guide's territory.

---

## Where to go next

- **Operating the bench day-to-day** —
  [`docs/operator-guide.md`](operator-guide.md).
- **Something broke** —
  [`docs/troubleshooting.md`](troubleshooting.md).
- **Understanding what the broker exposes** —
  [`docs/broker-api.md`](broker-api.md).
- **Understanding why the driver had to be patched** —
  [`docs/design/mcp251x-driver-patches.md`](design/mcp251x-driver-patches.md).
