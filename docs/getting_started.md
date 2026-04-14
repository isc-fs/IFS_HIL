# Getting Started — Replicating the HIL Bench on a New Raspberry Pi

This guide covers everything needed to bring up the HIL bench from a fresh
Raspberry Pi OS image.

---

## 1. Requirements

| Item | Details |
|------|---------|
| Raspberry Pi | Model 3B+ or 4, running Raspberry Pi OS Lite (64-bit recommended) |
| BACKPLANE_HIL PCB | Connected to the RPi 40-pin header |
| Network | RPi reachable over LAN (for dashboard access and SSH) |
| Host machine | Any machine that can SSH into the RPi |

---

## 2. Raspberry Pi OS configuration

Enable the required hardware interfaces with `raspi-config`:

```bash
sudo raspi-config
```

Navigate to **Interface Options** and enable:

- **SPI** — used by DAC80504, MCP3208, MCP2515, nRF24L01
- **I2C** — used by INA226, TCA9555

Reboot after saving.

Verify the interfaces are up:

```bash
ls /dev/spidev0.* /dev/i2c-*
# Expected: /dev/spidev0.0  /dev/i2c-1
```

---

## 3. Clone the repository

```bash
git clone https://github.com/isc-fs/IFS08_HIL.git
cd IFS08_HIL
```

If you want a specific branch:

```bash
git checkout dev
```

---

## 4. Install Python dependencies

```bash
pip install -e .
```

This installs all packages listed in `pyproject.toml` (`spidev`, `smbus2`,
`RPi.GPIO`, `flask`, `pytest`, etc.) and registers the `hil` CLI entry point.

> **Note:** `RPi.GPIO` and `spidev` require root or membership in the `gpio`
> and `spi` groups. Add your user once:
>
> ```bash
> sudo usermod -aG gpio,spi,i2c $USER
> # log out and back in for the groups to take effect
> ```

---

## 5. Start the observability dashboard

```bash
python3 dashboard/app.py
```

The dashboard polls all on-board ICs every 2 seconds and serves a web UI at:

```
http://<rpi-ip>:8080
```

From the dashboard you can:
- Monitor PSU status and toggle PS_ON#
- Read INA226 current / power per MLC carrier slot and toggle carrier relays
- Set DAC80504 output voltages (0–3.3 V per channel)
- Read MCP3208 ADC channels
- Switch MCP2515 CAN controller operating modes
- Inspect TCA9555 GPIO expander state

To run in the background and survive SSH disconnection:

```bash
nohup python3 dashboard/app.py > /tmp/dashboard.log 2>&1 &
```

---

## 6. Auto-start with systemd (optional)

A service unit is provided at `infra/systemd/hil-agent.service`. Adapt the
`User` and `WorkingDirectory` fields to match your setup, then install it:

```bash
# Edit the unit file to match your username and repo path
nano infra/systemd/hil-agent.service

sudo cp infra/systemd/hil-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hil-agent
sudo systemctl status hil-agent
```

To have the dashboard start on boot instead, create a dedicated unit:

```bash
sudo tee /etc/systemd/system/hil-dashboard.service << EOF
[Unit]
Description=HIL Observability Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/IFS08_HIL
ExecStart=/usr/bin/python3 dashboard/app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now hil-dashboard
```

---

## 7. Install udev rules (optional)

Udev rules give stable names to the ST-Link programmer and CAN adapters:

```bash
sudo cp infra/udev/99-hil.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

---

## 8. Run the hardware test suite

With the BACKPLANE_HIL PCB connected and the dashboard **stopped** (both
processes share the SPI bus):

```bash
pytest tests/hil/ -v
```

Expected: all tests pass against the on-board ICs. Tests that require a
connected ECU or a flashed CAN bootloader are skipped automatically when the
target is absent.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `No module named 'spidev'` | Dependency not installed | `pip install -e .` |
| `SPI not found / Permission denied` | SPI not enabled or wrong group | Enable SPI in `raspi-config`; add user to `spi` group |
| Dashboard shows all ICs as not responding | SPI/I2C not enabled | Check `raspi-config` and reboot |
| DAC outputs stuck at 0 V | Old driver without register fixes | Ensure you are on `dev` or later; the fixes are in `tools/dac80504.py` |
| CAN controllers stuck in sleep mode | Stale state from a previous session | Dashboard restart re-issues `reset()` automatically |
| `Address already in use` on port 8080 | Another dashboard instance running | `pkill -f app.py` |
