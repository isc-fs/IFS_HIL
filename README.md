# HIL Test Bench Skeleton

This repository seeds a Raspberry Pi based hardware-in-the-loop (HIL) bench that builds STM32 firmware, flashes the ECU, and verifies behaviour with automated tests.

## Quickstart
- Install system dependencies: `./scripts/bootstrap_pi.sh /srv/hil`
- Create a virtual environment and install the project in editable mode:
  - `python3 -m venv .venv && source .venv/bin/activate`
  - `pip install -e .`
- Inspect the helper CLI: `hil --help`
- Trigger the full bench workflow: `./scripts/run_hil_job.sh`

## `hil` CLI Usage
The Typer-based CLI offers a few placeholder commands to simulate end-to-end flows.

```bash
# Flash the ECU with a firmware binary
hil flash --firmware build/firmware/hil_firmware.bin --target stm32f103

# Probe CAN connectivity
hil probe --channel can0

# Toggle a power rail
hil power --target ecu --state true
```

## Project Layout
- `firmware/` — Minimal STM32 C project compiled with CMake.
- `tools/` — Python utilities (flash, CAN probe, power control, CLI entrypoint).
- `scripts/` — Automation scripts for bootstrapping, systemd integration, and running jobs.
- `infra/` — systemd unit and udev rules applied on the Pi.
- `tests/` — Placeholder pytest suite executed after flashing.
- `docker/` — Container definitions for build, flash, and test environments.
- `configs/` — YAML templates describing ECUs and agent behaviour.
- `.github/workflows/` — CI pipeline targeting the Raspberry Pi runner.
- `docs/` — Architecture notes and onboarding material.
