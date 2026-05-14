# AMS HIL tests

Bench-side implementation of the
[`IFS08-CE-AMS/docs/HIL_TESTS.md`](https://github.com/isc-fs/IFS08-CE-AMS/blob/dev/docs/HIL_TESTS.md)
test plan, adapted to the IFS08_HIL bench's actual capabilities.

## Quick start

On the Pi:

```sh
cd ~/IFS08_HIL
# build (or rsync from your dev machine) the AMS firmware first, then:
AMS_FIRMWARE_BIN=/tmp/AMS.bin pytest tests/hil/ams/ -v
```

The fixtures need a healthy bench (broker socket reachable, BL-flashed
carrier in the slot named by `ams_profile.yaml::mlc_slot`). They auto-skip
cleanly if any of that is missing, so off-bench `pytest tests/` stays green.

## What this directory holds

```
tests/hil/ams/
├── README.md            ← you are here
├── ams_profile.yaml     ← thresholds, cadences, bus mapping, slot index
├── conftest.py          ← fixtures: mlc_powered, bms_emulator, acu, …
├── test_block_a_boot.py ← HIL-002, 003 (and HIL-004 placeholder)
├── test_block_b_safety.py    ← not implemented yet
├── test_block_c_fsm.py       ← not implemented yet
├── test_block_d_comms.py     ← not implemented yet
├── test_block_e_bootloader.py ← not implemented yet
└── test_block_f_soak.py      ← not implemented yet
```

Generic helpers (BMS emulator, ACU stimulus, CAN observer, flasher wrapper)
live under `tools/firmware_test/` so VCU / MicroDV tests can reuse them.

## Bench-side prerequisites (one-off)

| Check | How |
|---|---|
| Broker + dashboard services up | `systemctl is-active hil-broker hil-dashboard` |
| BL-flashed carrier in the slot | `ams_profile.mlc_slot` matches physical position |
| `can-flasher` on PATH | `can-flasher --version` |
| `python-can` installed | `python3 -c "import can"` |
| `pyyaml` installed | `python3 -c "import yaml"` |

The fixtures `pytest.skip` rather than fail when these are missing, so they
won't false-fail on a degraded bench — but you'll see a lot of `SKIPPED`.

## What we can run vs what we can't

The AMS firmware on `dev` hard-codes pins that **the MAIN_LITE carrier does
not route to the bench connector** (PD3/PD4/PD5 relays, PE9 SDC, PF11 current
sense, PF13 AMS_OK, PG7 charge button, PA2/PA3 USART2). Until either:

- the firmware is re-pinned to MAIN_LITE GPIOs, or
- a Nucleo-H733ZG with breakouts is wired into the rig,

…the bench can run only CAN-observable tests. That covers Block A (boot via
BL), Block D (CAN cadence + frame parsing — once the firmware emits classic
CAN frames the MCP2515 can decode), and Block E (boot-trigger / BL
integration). Blocks B, C, and F that depend on relay output or SDC input
read-back are deferred.

| Test ID | Status | Notes |
|---|---|---|
| HIL-001 | ⏸️ deferred | Needs SWD to erase flash; not routine on this bench |
| HIL-002 | ✅ implemented | `test_block_a_boot.py::TestBlBringUp::test_bl_discover` |
| HIL-003 | ✅ implemented | needs `AMS_FIRMWARE_BIN` env var or `/tmp/AMS.bin` |
| HIL-004 | ⏸️ placeholder | needs UART access or FDCAN1-TX cadence (see Block D) |
| HIL-005..008 | ⏸️ deferred | SWD / GDB / flash-dump heavy |
| HIL-009 | ⏸️ deferred | needs GDB breakpoint in `HAL_FDCAN_RxFifo0Callback` |
| HIL-010..019 (Block B) | ⏸️ deferred | needs relay-output read-back / GDB |
| HIL-020..029 (Block C) | ⏸️ partial — FSM state inferable from TX cadence | |
| HIL-030..040 (Block D) | 🔜 next workstream | CAN cadence + parsing — fully bench-doable |
| HIL-041..047 (Block E) | 🔜 next workstream | CAN-only |
| HIL-050..055 (Block F) | ⏸️ deferred | soak runs need UART or TX cadence as a heartbeat |

## Tuning

Every threshold in the tests reads from `ams_profile.yaml`. Adjust there,
not in the test files. The profile is loaded once per session by the
`ams_profile` fixture.

When the firmware is re-pinned, the relevant block of the profile (and the
corresponding test files) get filled in. The structure stays.

## Adding a new test

1. Pick the block file (or add a new one).
2. Use the existing fixtures — `mlc_powered`, `bms_emulator`, `acu`,
   `observe_acu`, `observe_bms`, `flasher`. They handle setup/teardown.
3. Pull thresholds from the `ams_profile` fixture; don't hardcode.
4. Name the test `test_hil_NNN_*` so the HIL ID is searchable.

## Reference

- AMS firmware: <https://github.com/isc-fs/IFS08-CE-AMS> (`dev` branch)
- AMS test plan: [`docs/HIL_TESTS.md`](https://github.com/isc-fs/IFS08-CE-AMS/blob/dev/docs/HIL_TESTS.md)
- AMS CAN map: [`docs/CAN_MAP.md`](https://github.com/isc-fs/IFS08-CE-AMS/blob/dev/docs/CAN_MAP.md)
- Bench hardware reference: [`../../../docs/hardware-reference.md`](../../../docs/hardware-reference.md)
- Bench operator guide: [`../../../docs/operator-guide.md`](../../../docs/operator-guide.md)
