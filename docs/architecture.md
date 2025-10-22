# HIL Architecture

## Overview
This Raspberry Pi powered bench provisions firmware builds, flashes the device under test, and validates behaviour through automated tests. The Pi orchestrates tools such as CMake, OpenOCD, and pytest while archiving all run-time logs.

## Components
- **Firmware toolchain**: A minimal STM32 CMake project builds firmware artifacts.
- **HIL agent**: A systemd-managed script triggers builds, flashing, and tests and records the results for review.
- **Hardware services**: udev rules and helper scripts provide stable access to ST-Link programmers, CAN interfaces, and GPIO-controlled power rails.
- **Containers**: Dockerfiles pin toolchain versions for building, flashing, and automated testing.

## Data Flow
1. The agent pulls or receives a job definition and configures the firmware build.
2. Build outputs are passed to the flashing utilities which interact with the ECU via ST-Link.
3. Post-flash diagnostics run through the CAN probe and pytest-based verification suite.
4. Logs persist under `/hil/logs` and surface back to CI for triage.
