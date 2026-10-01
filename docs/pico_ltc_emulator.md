# LTC6820/LTC6811 emulator (Pi Pico)

Extends the bench so the MLC carrier's SPI master sees a believable
LTC6820 + 10× LTC6811 daisy chain even when no AMS daughterboard is
installed.

## Why this exists

The AMS daughterboard (the only place the real LTC chain lives) is not
on the HIL bench — it's only on the actual car. To boot AMS firmware
on a stand-alone MLC carrier we either:

1. **Build with `-DAMS_BMS_HIL_STUB=1`**, which compiles out the
   entire `bms_poll_task.cpp` LTC chain path and seeds nominal BMS
   data directly into `BmsService` (see firmware
   `bms_service.cpp::seed_for_hil_stub`). Fast, but bypasses every
   line of the real LTC SPI decoder — including PEC15 verification,
   bit ordering, and partial-update atomicity, which are exactly the
   bugs that hurt at integration time. Also has its own reliability
   issues (`isc-fs/IFS08-CE-AMS#204` — task starvation).

2. **Provide an LTC chain on the bench** so firmware can talk to it
   over SPI. The Pico emulator is the cheapest way to do this:
   ~$4 of hardware, all the timing constraints handled by an RP2040
   PIO SPI slave (mode 3), no PCB changes.

This document covers option 2. Option 1 is gone: per
`tests/hil/ams/test_block_e_ltc.py`, the AMS has run its real LTC path
in every build since the stub was removed (#207), so on the bench the
Pico is what answers it.

## Architecture

```
                                                            +-------------+
              USB (CDC commands + flash)                    |             |
   +-------+ <-----------------------------------------+   |             |
   |  Pi   |                                            |   |  Pi Pico    |
   |       |    "SET_CELL m c mV"                       +-->|  (RP2040)   |
   +---^---+                                                |             |
       |                                                    | SPI slave   |
       | broker, dashboard, pytest                          | LTC6811 emu |
       |                                                    +------+------+
                                                                   |
                                                                   | SCK/MOSI/MISO/GND via J8,
                                                                   | + a CS tap to GP17
                                                                   v
                                                            +-------------+
                                                            | MLC carrier |
                                                            |   STM32     |
                                                            |  SPI master |
                                                            +-------------+
```

The Pi orchestrates the Pico over the same USB cable that powers it.
The Pico, in turn, presents itself to the MLC as an LTC6820-style SPI
slave — a chain of 10 LTC6811 voltage-monitor ICs in daisy-chain
configuration.

## What the firmware sees

After `BmsPollTask`'s LTC initialisation completes (WRCFGA on the
chain), polls happen every `kBmsPollVoltMs`:

1. **Voltage poll cycle**: `ADCV` start-conversion → wait for tCONV →
   four reads `RDCVA/B/C/D`. Each read returns 4 × 80 = 320 bytes
   total (10 chips × 8 bytes per group, 4 groups). Each 8-byte chunk
   is 3 × 2-byte cell readings + 2-byte PEC15.
2. **Temperature scan** (every `kBmsPollTempMs`): the NTCs reach the
   LTC's GPIO1 through an ADG731 32:1 mux. For each slot the AMS writes
   the mux selector with `WRCOMM` + `STCOMM`, converts with `ADAX`, and
   reads AUX1 with `RDAUXA` — nothing else from the aux registers. On
   the wire `RDAUXA` therefore far outnumbers any `RDCV` group.

`BmsService::update_from_ltc_response` walks the 320 bytes, verifies
each per-IC PEC15, and commits voltages to `state_.cell_mV[m][c]`. A
single PEC15 miss invalidates that IC's slice — partial-update
atomicity is per-IC.

## Pico protocol — what the firmware emulates

| Command | Code (11-bit) | Direction | Bytes (after 11-bit cmd + PEC15) |
|---|---|---|---|
| WRCFGA | 0x001 | M→S | 6 cfg bytes per chip × 10 chips, PEC15 per chip |
| ADCV   | 0x260..0x370 (mode/discharge bits) | M→S | none — just an ADCV start |
| RDCVA  | 0x004 | M←S | 8 × 10 = 80 (cells 1..3 per chip) |
| RDCVB  | 0x006 | M←S | 80 (cells 4..6) |
| RDCVC  | 0x008 | M←S | 80 (cells 7..9) |
| RDCVD  | 0x00A | M←S | 80 (cells 10..12) |
| RDAUXA | 0x00C | M←S | 80 (GPIO 1..3; AUX1 = the NTC on the current mux channel) |
| RDAUXB | 0x00E | M←S | 80 (GPIO 4..5 + ref2, a fixed 3.0 V) |
| WRCOMM | 0x721 | M→S | 6 COMM bytes per chip — snooped for the ADG731 selector |

The reads return the stored cell/temp state with a valid PEC15 per
chip. The opcode is decoded from the first two command bytes, so the
response is chosen before the first data byte is clocked. `WRCOMM` is
snooped: when the first chip's payload is a real mux write, `RDAUXA`'s
AUX1 switches to the temperature stored for that channel. Everything
else — `ADAX`, `STCOMM`, `RDCFGA`, `RDSTATA/B`, `RDSID`, the discharge
bits in `WRCFGA`, … — is accepted but has no effect and no response of
its own: a read the Pico does not model gets whichever `RDCV*` /
`RDAUX*` buffer it served last, PEC-valid but meaningless.

## Pi-side serial command protocol

CDC line-oriented (NL terminator), case-insensitive command names,
whitespace-separated args, hex or decimal accepted for integers.

| Command | Args | Reply | Purpose |
|---|---|---|---|
| `PING` | — | `PONG <fw_version>` | health check (`PONG 0.7.0` today) |
| `STATUS` | — | `OK n_cmds_rx= n_spi_xact= last_cmd= n_valid_cmds= last_ltc_cmd= n_cs_cycles= rx_bytes= tx_stall_cmd= stop_mask= adg731_ch= last_rx=<8 bytes>` | counters + state |
| `SET_CELL` | m c mV | `OK` | set one cell voltage (mV); `m` = chain position 0..9, `c` = 0..11 |
| `SET_TEMP` | m s dC | `OK` | set one temperature (deci-°C); `s` = ADG731 mux channel 0..31 |
| `SET_ALL_CELLS` | mV | `OK` | bulk write |
| `SET_ALL_TEMPS` | dC | `OK` | bulk write |
| `RESET_STATE` | — | `OK` | back to seed defaults; also clears `STOP_REPLY` and zeroes `n_spi_xact` / `last_cmd` |
| `STOP_REPLY` | mask | `OK` | 5-bit module mask: bit N sends `0xFF` for chain positions 2N and 2N+1, so their PEC fails at the AMS |
| `RESUME_ALL` | — | `OK` | clear the `STOP_REPLY` mask; cell/temp state untouched |
| `STATS_CMDS` | — | `OK n=<N> lost=<N>`, then one `CMD 0x<op> count= len_last= len_min= len_max=` line per opcode | per-opcode transaction count and bytes clocked (24 opcodes max; `lost` counts the rest) |
| `STATS_CMDS_RESET` | — | `OK` | zero those counters |
| `DUMP` | cmd | `OK cmd= len=84 bytes=…` | the prebuilt response for one `RDCV*` / `RDAUX*` opcode |
| `DUMP_TX` | — | `OK cmd= len= bytes=…` | bytes pushed to MISO in the last transaction |
| `DUMP_RX` | — | `OK len= bytes=…` | bytes received on MOSI in the last transaction |
| `BSL` | — | `OK -- bye` then re-enumerates as USB MSC | reboot into the RP2040 bootloader |

A rejected command answers with a single `ERR …` line. Defaults match
`seed_for_hil_stub`: 3750 mV per cell, 25.0 °C (250 deci-°C) per temp
sensor.

From Python, use `PicoLtcClient` in
[`tools/pico_ltc_emulator/host/pico_ltc_client.py`](../tools/pico_ltc_emulator/host/pico_ltc_client.py)
(it finds the Pico by USB vendor `0x2E8A`). Its `set_module_cell` /
`set_module_temp` (aliases `inject_cell_v` / `inject_cell_t`) take AMS
addressing — module 0..4, cell 0..18, NTC slot 0..19 — and map it onto
the chain with the AMS split `CellsPerLtcUpper = 9` /
`CellsPerLtcLower = 10`; NTC slot N lands on the module's upper LTC at
mux channel N (N < 10) or N + 6. AMS tests get a client from the
`pico_emu` fixture in `tests/hil/ams/conftest.py`, which skips when the
Pico doesn't answer `PING`. The client has no `STATS_CMDS` helper — its
reply is multi-line.

## Wiring

MLC2's STM32 SPI1 bus is broken out on the **J8 patch header** on the
BACKPLANE_HIL PCB (a 2x02 2.54 mm pin header with SCK/MISO/MOSI/GND):
Pico GP18 = SCK, GP19 = MISO (driven by the Pico), GP16 = MOSI, plus
GND — no soldering to the carrier connector required.

The LTC6820 chip-select (`STM32 PA4`, see firmware
`Core/Inc/main.h:68`) is **NOT** broken out on J8, but the firmware
needs it on **GP17**: the PIO slave frames every transaction on CS
edges (a CS rise resets the command decode and re-arms the TX pad), and
the main loop services USB only while CS is high. Tie GP17 low, as the
first cut allowed, and the Pico decodes one command and then never
answers on USB. Tap the MLC2 daughterboard's `LTC6820_CS` net to GP17.
How bench-01's CS is tapped is not recorded in this repo.

Detailed wiring table: [`tools/pico_ltc_emulator/README.md`](../tools/pico_ltc_emulator/README.md#wiring-target-mlc2-slot)
(its CS-tied-low option A predates the PIO slave and no longer works).

## Build

Needs the Pico SDK (`PICO_SDK_PATH` in the environment, or
`-DPICO_SDK_PATH=…`), `cmake` and `arm-none-eabi-gcc`. Build in a
fresh directory — the SDK path is cached in `CMakeCache.txt`:

```sh
cd tools/pico_ltc_emulator/firmware
rm -rf build
cmake -B build -DPICO_SDK_PATH="$PICO_SDK_PATH"
cmake --build build -j
# -> build/pico_ltc_emulator.uf2
```

The Pico hangs off the bench Pi's USB, so that is where it is flashed.
`scripts/sync_to_pi.sh` skips `build/` (and `*.uf2` is gitignored):
build on the Pi, or copy the `.uf2` across.

## First flash vs subsequent flashes

First flash (a Pico not yet running this firmware): hold BOOTSEL while
plugging USB → MSC mode → `tools/pico_ltc_emulator/host/flash_pico.sh
<new.uf2>`. After this, subsequent flashes don't need a button — just
run `flash_pico.sh <new.uf2>` again. The wrapper finds the Pico's CDC
port (USB vendor `2e8a`, via `udevadm`), sends `BSL`, waits 1.5 s, then
runs `picotool load -fx`, which loads the UF2 and starts it. With no
CDC port it falls back to `picotool reboot -f -u`. Needs `picotool`
(`sudo apt install picotool`). Confirm with `PING` → `PONG 0.7.0`.

## Status

In use on bench-01 against a live AMS on MLC2: the bench declares
`stim-cells`, `stim-temps` and `fault-module-silent` on the strength of
it. Fixes that change how older results read:

| PR | Firmware | Fix |
|---|---|---|
| #115 | (host client) | `PicoLtcClient`'s module → chain cell split was inverted (10/9); now `CellsPerLtcUpper = 9` / `CellsPerLtcLower = 10`, as in `ams_config.hpp`. Before it, some injections hit the wrong cell or a channel the AMS never reads, and cell 18 was unreachable. |
| #118 | 0.5.0 | NTC AUX voltages come from the AMS's own R-T table (Fenghua CMFB103F3950, 6.8 kΩ pull-up to VREF2 = 3.0 V) instead of a beta fit with a 10 kΩ pull-up. Every seeded temperature used to read 3–10 °C high — 25 °C read back as 34 °C (#117). |
| #133 | 0.6.0 | `STATS_CMDS` / `STATS_CMDS_RESET`, and `tx_stall_cmd` in `STATUS`. |
| #134 | 0.7.0 | The opcode is decoded at RX byte 2, so chain position 0 serves the current command. Before, chip 0 carried the previous command's response, and because the AMS reads `RDAUXA` far more often than any `RDCV` group, module 0's cell voltages tracked the NTC curve (#116). |

`tx_stall_cmd` should stay 0; a non-zero count means the byte-2 parse
missed its ~20 µs budget and MISO was wrong for that transaction.

**Open — #135.** A C fall-through in `parse_ltc_cmd()` sets the WRCOMM
snoop flag for eleven opcodes (`WRCFGA`, `RDCFGA`, `RDSTATA/B`,
`WRSCTRL`, `WRPWM`, `WRCFGB`, `RDCFGB`, `RDSID`, `PLADC` and `WRCOMM`),
not just `WRCOMM`. Any of them whose first payload byte starts with
nibble `0x8` retargets the emulated temperature mux.

**Gotcha — don't census commands by polling.** `DUMP_TX`, `DUMP_RX`
and `STATUS`'s `last_cmd` / `last_ltc_cmd` hold only the last
transaction, and a host round-trip over USB CDC takes ~50 ms while the
AMS reads in bursts, so polling them aliases — it once "showed" that
`RDCVA/B/C` were never sent and that only 9 chips were clocked. Use
`STATS_CMDS`: per opcode, the count and the bytes clocked (84 = 4 + 10 ×
8 for a full-chain read; a 9-chip read would be 76).

The #116 probes stay in `tools/`: `measure_116.py`, `verify_116.py`,
`bootcheck_116.py`, `pec_under_load.py`.

## Related

- AMS firmware: `Core/Src/app/bms_service.cpp` (LTC decoder),
  `Core/Src/app/bms_poll_task.cpp` (poll loop)
- HIL stub: removed (#207). It was the short-term bandage for
  `isc-fs/IFS08-CE-AMS#204` (BMS poll task wedge); the Pico emulator
  is the replacement
- BACKPLANE_HIL J8 patch header pinout:
  [`docs/BACKPLANE_HIL/pcb_analysis.json`](BACKPLANE_HIL/pcb_analysis.json)
  (`J8` entry; the KiCad directory is gitignored, so this exists only
  in a local copy)
