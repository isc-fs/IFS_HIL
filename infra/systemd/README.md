# `infra/systemd/`

Systemd units for the HIL bench. Copy into `/etc/systemd/system/` and
enable as needed.

## `hil-psu-on.service`

Asserts `PS_ON#` (GPIO7 = LOW) at early boot so the ATX PSU comes up
before anything that needs main-rail-powered chips. Complements the
firmware directive `gpio=7=op,dl` in `/boot/firmware/config.txt` —
the Pi 4's BCM2835 GPIO driver has "GPIO_OUT persistence" enabled, so
a prior userspace `pinctrl set 7 op dh` can stick across reboots and
leave PSU OFF even when firmware tried to assert it. This service
re-asserts LOW unconditionally in the userspace boot path.

Also forces GPIO8 back to `ip,pd` so the PWR_OK signal can be read
(legacy SPI0_CE0 pinmux sometimes reappears as output-low).

Install:

```sh
sudo cp hil-psu-on.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hil-psu-on.service
```

## `hil-can-up.service`

Brings `can0`, `can1`, `can2` up at 500 kbps with `txqueuelen=1000`
and `restart-ms=200`. The queue-length default of 10 overflows during
`can-flasher flash` and returns `ENOBUFS` mid-sector; 1000 comfortably
sustains a full-speed flash write. `restart-ms=200` enables the
kernel's automatic bus-off recovery so a transient bus event doesn't
leave the interface stuck DOWN.

Ordered `After=hil-psu-on.service` and `Before=hil-broker.service`, so
the broker always sees the interfaces already UP at the right bitrate.

Install:

```sh
sudo cp hil-can-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hil-can-up.service
```

## `hil-broker.service`

The broker daemon (see `docs/broker_migration_plan.md`). Starts after
`hil-psu-on.service` and `hil-can-up.service` so the PSU is stable and
the canN interfaces are already up before the broker opens
`/dev/spidev0.3` and binds SocketCAN sockets. Expects the
[patched mcp251x module](../kernel-module/mcp251x-patched/) to already
be installed and the `mcp2515-triple` dtoverlay to be active.

## Kernel netdev ↔ PCB CAN mapping

On this kernel, `mcp251x` probes the SPI children in *reverse* `reg`
order, so the kernel netdev names are **inverted** relative to the
PCB labels in `tools/hw_config.py`:

| kernel netdev | spi device | CS GPIO | PCB label | chip |
|---|---|---|---|---|
| `can0` | `spi0.2` | GPIO18 | **CAN3** | U21 |
| `can1` | `spi0.1` | GPIO17 | **CAN2** | U19 |
| `can2` | `spi0.0` | GPIO27 | **CAN1** | U17 |

The MLC1..MLC4 carriers are wired to PCB CAN1 → **kernel `can2`**.
