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
   ~$4 of hardware, all the timing constraints handled by the RP2040
   PIO + DMA SPI slave, no PCB changes.

This document covers option 2.

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
                                                                   | 4 wires (SCK/MOSI/MISO/GND
                                                                   |          via J8 patch header)
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
chain), polls happen at `kBmsPollVoltMs = 250 ms`:

1. **Voltage poll cycle**: `ADCV` start-conversion → wait for tCONV →
   four reads `RDCVA/B/C/D`. Each read returns 4 × 80 = 320 bytes
   total (10 chips × 8 bytes per group, 4 groups). Each 8-byte chunk
   is 3 × 2-byte cell readings + 2-byte PEC15.
2. **Temperature poll cycle** (every `kBmsPollTempMs`): GPIO/aux reads
   via `RDAUXA/B`.

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
| RDAUXA | 0x00C | M←S | 80 (GPIO 1..3) |
| RDAUXB | 0x00E | M←S | 80 (GPIO 4..5 + ref2) |

First-cut firmware implements all of the above, returning the
currently-stored cell/temp state with valid PEC15 trailers. Other
LTC6811 commands (RDSTATA/B, RDCFGA, balance discharge controls,
etc.) are stubbed with a fixed-byte response — enough for the AMS
poll loop to not crash; expand as needed.

## Pi-side serial command protocol

CDC line-oriented (NL terminator), case-insensitive command names,
whitespace-separated args, hex or decimal accepted for integers.

| Command | Args | Reply | Purpose |
|---|---|---|---|
| `PING` | — | `PONG <fw_version>` | health check |
| `STATUS` | — | `OK n_cmds_rx=<N> n_spi_xact=<N> last_cmd=<HEX>` | counters |
| `SET_CELL` | m c mV | `OK` | set one cell voltage (mV) |
| `SET_TEMP` | m s dC | `OK` | set one temp sensor (deci-°C) |
| `SET_ALL_CELLS` | mV | `OK` | bulk write |
| `SET_ALL_TEMPS` | dC | `OK` | bulk write |
| `RESET_STATE` | — | `OK` | back to seed defaults |
| `BSL` | — | `OK -- bye` then re-enumerates as USB MSC |

Defaults match `seed_for_hil_stub`: 3750 mV per cell, 25.0 °C
(250 deci-°C) per temp sensor.

## Wiring

The MLC1 carrier's STM32 SPI1 bus (PA5/6/7) is already exposed on the
**U22 nRF24 footprint** on the BACKPLANE_HIL PCB. Pop out the nRF24
module (if installed) and drop the Pico into U22's footprint via a
breakout board / female header — no soldering to the carrier
connector required.

The LTC6820 chip-select (`STM32 PA4`, see firmware
`Core/Inc/main.h:68`) is **NOT** broken out on U22 — U22 pad 4 is the
nRF24 CS GPIO, a different STM32 pin. Two paths:

- **CS-tied-low**: skip CS gating, rely on PEC15 + idle-gap framing.
  Works as long as SPI1 carries only LTC traffic (true today since
  the firmware doesn't initialise an nRF24 driver).
- **Tap PA4**: magnet-wire from the MLC1 daughterboard `LTC6820_CS`
  net to a Pico GPIO. Proper semantics; required if nRF24 is ever
  enabled alongside LTC.

Detailed wiring table: [`tools/pico_ltc_emulator/README.md`](../tools/pico_ltc_emulator/README.md#wiring-target-mlc1-slot).

## First flash vs subsequent flashes

First flash: hold BOOTSEL on Pico while plugging USB → MSC mode →
`flash_pico.sh <new.uf2>`. After this, the firmware exposes the
`BSL\n` CDC command, and subsequent flashes don't need a button —
just `flash_pico.sh <new.uf2>` again. The wrapper sends `BSL`, waits
for MSC re-enumeration, copies the new UF2, and the Pico
auto-reboots into the new firmware.

## What's blocking

The first cut firmware is scaffolded but not yet exercised against a
real MLC carrier's SPI master. Validation order:

1. **Loopback test** — bench Pi as SPI master into Pico SPI slave,
   verify the Pico's RDCVA response decodes correctly with a Python
   PEC15 verifier.
2. **MLC integration test** — wire to MLC2 J8, flash a
   non-HIL_STUB AMS build, watch for `BmsState.pack_voltage_mV` ≈
   stub seed and `module_online_mask = 0x1F`.
3. **Fault injection** — change one cell to 4500 mV via `SET_CELL`,
   confirm the firmware's overvoltage predicate trips Error within
   `kSafetyPeriodMs + telemetry latency`.

Once step 3 passes, the bench can drop `-DAMS_BMS_HIL_STUB=1` for
the AMS test suite and exercise the real LTC code path.

## Related

- AMS firmware: `Core/Src/app/bms_service.cpp` (LTC decoder),
  `Core/Src/app/bms_poll_task.cpp` (poll loop)
- HIL stub status: `isc-fs/IFS08-CE-AMS#204` (BMS poll task wedge —
  Pico emulator is the long-term replacement, this is the short-term
  bandage)
- BACKPLANE_HIL J8 patch header pinout:
  [`docs/BACKPLANE_HIL/pcb_analysis.json`](BACKPLANE_HIL/pcb_analysis.json)
  (`J8` entry)
