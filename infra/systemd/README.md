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

Install:

```sh
sudo cp hil-broker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hil-broker.service
```

## `hil-dashboard.service`

Flask observability UI at `http://<pi-ip>:8080/`. Ordered
`After=hil-broker.service` and `Wants=hil-broker.service` — the
dashboard is a broker client (polls every 2 s over the Unix socket)
and tolerates broker absence by colouring affected panels red, so it
stays reachable for diagnostics even if the broker dies.

Replaces the legacy `nohup python3 dashboard/app.py &` pattern. Logs
go to the systemd journal (`journalctl -u hil-dashboard -f`), not
`/tmp/dashboard.log`.

Install:

```sh
sudo cp hil-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hil-dashboard.service
```

If you have a stray `nohup` instance still running, the new service
will fail to bind 8080 — kill it first:

```sh
pkill -f dashboard/app.py
sudo systemctl restart hil-dashboard.service
```

## `hil-bench-watchdog.service` + `.timer`

Verifies the bench against its descriptor every 5 minutes and recovers it
if it has drifted. Install BOTH — the service alone never fires.

HIL runs are unattended. bench-01's DAC80504s stop answering their device
id often enough (five times in one day, IFS_HIL#124) that waiting for a
human to notice is not a plan, and `hil-test.yml`'s preflight only
recovers when a run happens to start — too late for the developer who
triggered it, and never for an idle bench.

The recovery ladder is `tools/bench.py recover`: level 1 restarts the
broker (~6 s, often enough), level 2 power-on-resets the rails and rebuilds
CAN behind them (~27 s). It stops at the first rung that works.

**It never acts on a busy bench.** The lock is taken non-blocking, so a run
mid-flash or mid-suite is left strictly alone — level 2 cycles the rails,
and doing that during a flash is the interrupted write that leaves an H7
unrecoverable (F-077). Waiting for the lock would be just as wrong: the
watchdog would queue behind a 90-minute soak and then fire into the next
run.

Runs `--verbose`, so the journal records healthy checks too. That baseline
is the point: a bench recovering on *every* cycle is a worsening fault, and
that is only visible against a run of quiet passes.

```sh
sudo cp hil-bench-watchdog.service hil-bench-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hil-bench-watchdog.timer
journalctl -u hil-bench-watchdog -f
```

## `hil-agent.service` — NOT installed

Shipped here but deliberately **not** part of a bench build, which is why
[`docs/getting-started.md`](../../docs/getting-started.md) installs four units
and this directory holds five.

It execs `/srv/hil/scripts/run_hil_job.sh`, which does not exist in this
repository — a leftover from an earlier design where the bench polled for work
(see the `agent:` block in [`configs/hil_agent.yaml`](../../configs/hil_agent.yaml)).
That approach is superseded by GitHub-dispatched runs
(`.github/workflows/hil-test.yml`), where a self-hosted runner is the worker and
routing is decided from the bench descriptors.

Do not install or enable it. Left in the tree pending a decision to delete it
along with `configs/hil_agent.yaml`.

## Service dependency order

```mermaid
flowchart LR
    PSU["hil-psu-on.service<br/>(oneshot, sysinit)"]
    CAN["hil-can-up.service<br/>(oneshot)"]
    BROKER["hil-broker.service<br/>(long-running)"]
    DASH["hil-dashboard.service<br/>(long-running)"]

    PSU --> CAN --> BROKER --> DASH

    classDef oneshot fill:#fffde7,stroke:#f9a825,color:#6c4d00
    classDef daemon fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef ui fill:#e3f2fd,stroke:#1976d2,color:#0d47a1

    class PSU,CAN oneshot
    class BROKER daemon
    class DASH ui
```

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
