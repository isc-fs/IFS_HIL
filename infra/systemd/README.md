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

## `hil-broker.service`

The broker daemon (see `docs/broker_migration_plan.md`). Starts after
`hil-psu-on.service` so the PSU is stable before the broker opens
`/dev/spidev0.3` and I²C. Expects the
[patched mcp251x module](../kernel-module/mcp251x-patched/) to already
be installed and the `mcp2515-triple` dtoverlay to be active.
