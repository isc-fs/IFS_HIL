# IF08_HIL – Hardware-in-the-Loop Test Environment

This project implements a **Hardware-in-the-Loop (HIL)** testing framework for an **STM32-based ECU**, fully automated through a **Raspberry Pi** acting as the test orchestrator.

It allows continuous integration of embedded firmware by automatically:
1. Building new firmware revisions inside a reproducible Docker container.
2. Flashing the ECU through OpenOCD or DFU.
3. Simulating its environment (sensors, faults, CAN messages).
4. Running automated functional tests.
5. Producing structured reports for validation and regression tracking.

---

## ⚙️ Overview

**HIL Concept:**  
The ECU (real hardware) is connected to a simulated environment (virtual sensors, actuators, and power system). The Raspberry Pi manages both the build/test workflow and the simulation interface.

This setup enables safe, repeatable testing of control logic and communication features before integration into the real system.

---

## System Architecture

| Component | Role |
|------------|------|
| **ECU (STM32)** | Device under test; executes the firmware being validated. |
| **Raspberry Pi (host)** | Orchestrates build, flash, and test. Runs simulation loops and analysis. |
| **Docker container** | Provides a reproducible build & test environment (GCC toolchain, OpenOCD, pytest). |
| **Test bench hardware** | Includes CAN adapter, DAC/ADC boards, relays for fault injection, PSU control, etc. |

---

## Repository Structure

Top-level repository layout and brief explanations:

```text
IF08_HIL/
├── .github/                 # CI/CD workflows
├── configs/                 # Hardware and test configuration YAMLs
├── docker/                  # Toolchain and build environment
│   ├── Dockerfile
│   ├── passthrough.sh
│   └── toolchain-arm-none-eabi.cmake
├── docs/                    # Design documentation and diagrams
├── firmware/                # STM32 firmware source code
│   ├── src/
│   ├── include/
│   └── CMakeLists.txt
├── infra/                   # Raspberry Pi system-level integration
│   ├── systemd/
│   └── udev/
├── scripts/                 # High-level automation (build, flash, test)
│   ├── build.sh
│   ├── flash_openocd.sh
│   ├── test.sh
│   └── run_hil_job.sh
├── tests/                   # Automated HIL tests (pytest)
│   └── hil/
│       ├── test_example.py
│       ├── test_can_faults.py
│       └── …
├── tools/                   # Reusable Python utilities (CAN, flashing, PSU control)
│   ├── can_probe.py
│   ├── flash.py
│   ├── power_ctl.py
│   └── init.py
├── pyproject.toml           # Python dependencies (optional)
├── README.md                # You are here
└── .gitignore
```

Notable items:
- docker/: reproducible build image and toolchain configuration.
- firmware/: firmware sources and CMake build entrypoint.
- scripts/: convenience wrappers for CI and on-device automation.
- tests/: pytest-based HIL test cases and fixtures.
- tools/: small Python utilities used by tests and scripts.
- configs/: hardware mappings and test parameter YAMLs.
- infra/: Raspberry Pi integration (systemd units, udev rules).

