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
├── README.md                  ← you are here
├── ams_profile.yaml           ← thresholds, cadences, bus mapping, slot index
├── conftest.py                ← fixtures: mlc_powered, bms_emulator, acu, observe_acu,
│                                observe_bms, wait_for_state, current_state, flasher
├── test_block_a_boot.py       ← HIL-002, 003, 004
├── test_block_b_safety.py     ← HIL-014, 015, 016, 017
├── test_block_c_fsm.py        ← HIL-020..028
├── test_block_d_comms.py      ← HIL-030, 031, 032, 033, 035, 036, 037, 038, 040
├── test_block_e_bootloader.py ← HIL-041, 042, 043 (×6), 044, 046, 047
└── test_block_f_soak.py       ← not implemented yet
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

The AMS firmware on `dev` (post-PR-#81, post-PR-#80) now:
- emits **classic CAN frames** on both FDCAN1 and FDCAN2 — bench MCP2515s
  can decode TX traffic ✓
- drops the UART debug path in favour of three telemetry frames on FDCAN1:
  `0x4A0` (state + cell V), `0x4A1` (pack V + current), `0x4A2` (temps +
  DC bus + heartbeat), all at 500 ms cadence ✓

Combined with the BMS slave emulator and ACU stimulus on the bench side,
this brings Blocks A, B (subset), C, D, and E into bench-doable territory.

What's *still* deferred is anything that requires direct access to the
STM32 pins the AMS firmware hard-codes but the MAIN_LITE carrier does not
route to the bench: **PD3/PD4/PD5** (relays), **PE9** (SDC), **PF11**
(current ADC), **PF13** (AMS_OK), **PG7** (charge button), **PA2/PA3**
(USART2, now unused by firmware anyway). And anything that needs SWD/GDB
(HIL-001, 005, 008, 010, 011, 013).

Block-by-block status is in the table below.

| Test ID | Status | Notes |
|---|---|---|
| HIL-001 | ⏸️ deferred | needs SWD to erase flash |
| HIL-002 | ✅ done | `test_block_a_boot.py::TestBlBringUp::test_bl_discover` |
| HIL-003 | ✅ done | needs `AMS_FIRMWARE_BIN` or `/tmp/AMS.bin` |
| HIL-004 | ✅ done | observes `0x4A0` byte 0 = `FsmState.Start` after flash + jump |
| HIL-005..008 | ⏸️ deferred | SWD / GDB / flash-dump heavy |
| HIL-009 | ⏸️ deferred | needs GDB breakpoint in `HAL_FDCAN_RxFifo0Callback` |
| HIL-010..013 | ⏸️ deferred | need SafetyTask timing / GDB |
| HIL-014 | ✅ done | cell UV via BMS emulator → `0x4A0` state=Error |
| HIL-015 | ✅ done | cell OV via BMS emulator → `0x4A0` state=Error |
| HIL-016 | ✅ done | cell OT via BMS emulator → `0x4A0` state=Error |
| HIL-017 | ✅ done | BMS staleness via `stop_module()` → `0x4A0` state=Error |
| HIL-018 | ⏸️ deferred | PF11 current ADC not routed on MAIN_LITE |
| HIL-019 | ⏸️ deferred | PE9 SDC input not routed on MAIN_LITE |
| HIL-020 | ✅ done | start button → Precharge |
| HIL-021 | ✅ done | charger detect → Charge |
| HIL-022 | ✅ done | Precharge → Transition on DC bus high |
| HIL-023 | ✅ done | Precharge timeout → Error |
| HIL-024 | ✅ done | Transition → Run after hold |
| HIL-025 | ✅ done | Transition V drop → Error |
| HIL-026 | ✅ done | Run is terminal |
| HIL-027 | ✅ done | Charge is terminal |
| HIL-028 | ✅ done | Error sticky within boot |
| HIL-029 | ⏸️ deferred | needs software reset → app boots in Error (cross-boot persistence) |
| HIL-030 | ✅ done | BMS voltage poll cadence 250 ms |
| HIL-031 | ✅ done | BMS temp poll cadence 500 ms |
| HIL-032 | ✅ done | BMS V parsing → `0x4A0` min/max |
| HIL-033 | ✅ done | BMS temp parsing → `0x4A2` min/max |
| HIL-034 | ⏸️ deferred | `g_bms_rx_dropped_unknown` not in telemetry |
| HIL-035 | ✅ done | `0x100` DC bus → `0x4A2` dc_bus_V |
| HIL-036 | ✅ done | `0x600` start button → state transition |
| HIL-037 | ✅ done | `0x18FF50E7` → Charge transition |
| HIL-038 | ✅ done | `0x12C` ext TX cadence 500 ms in Run |
| HIL-039 | ⏸️ deferred | PF11 current ADC not routed |
| HIL-040 | ✅ done | charger-suppress: `0x12C` ext + BMS polls silent in Charge |
| HIL-041 | ✅ done | boot-trigger round-trip on FDCAN2 |
| HIL-042 | ✅ done | wrong-bus trigger (FDCAN1) ignored |
| HIL-043 | ✅ done | wrong-payload trigger ignored (6 sub-cases parametric) |
| HIL-044 | ✅ done | wrong-DLC trigger ignored (DLC 3, 5, 8) |
| HIL-045 | ⏸️ deferred | µs-resolution on PD3/4/5 + NRST; not routed |
| HIL-046 | ✅ done | BL one-shot; post-trigger power-cycle boots app |
| HIL-047 | ✅ done | flood of malformed + one valid |
| HIL-050..055 | ⏸️ deferred | soak/fuzz — feasible but long; build last |

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
