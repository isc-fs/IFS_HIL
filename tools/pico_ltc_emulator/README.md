# pico_ltc_emulator — LTC6820/LTC6811 chain emulator on a Raspberry Pi Pico

Adds a Pi Pico to the HIL bench so the MLC carrier's SPI master sees a
plausible LTC6820 + 10× LTC6811 daisy chain even with no AMS
daughterboard plugged in. Closes the gap that forces
`-DAMS_BMS_HIL_STUB=1` builds on the bench, and unblocks the AMS BMS
SPI decode coverage that the stub bypasses entirely.

See [`docs/pico_ltc_emulator.md`](../../docs/pico_ltc_emulator.md) for
the architecture, protocol notes, and AMS-side context.

## Layout

```
firmware/        Pico SDK C project (UF2 output)
  src/           main + SPI-slave + LTC protocol handler + USB-CDC cmd
  include/       tusb_config.h
  CMakeLists.txt
host/            Pi-side helpers
  pico_ltc_client.py  serial command client (set cell mV, set tempC, etc.)
  flash_pico.sh       picotool wrapper that uses the BSL command, no buttons
tests/           off-target unit tests (PEC15, command parser)
```

## Wiring (target: MLC2 slot)

MLC2's STM32 SPI1 bus is broken out on the **J8 patch header** (a
2x02 2.54 mm pin header on the BACKPLANE). 4 wires, no soldering to
the carrier connector itself.

| Pico GPIO | J8 pad | Net | Direction |
|---|---|---|---|
| GP18 (SPI0 SCK)  | pad 1 | `/SLOT2_SPI_SCK`  | MLC2 master → Pico (input) |
| GP19 (SPI0 TX)   | pad 2 | `/SLOT2_SPI_MISO` | Pico → MLC2 master (**slave drives**) |
| GP16 (SPI0 RX)   | pad 3 | `/SLOT2_SPI_MOSI` | MLC2 master → Pico (input) |
| any Pico GND     | pad 4 | GND               | reference |

Power: Pico VBUS via Pi USB. No extra power wire to J8 needed (J8
doesn't expose 3V3 anyway).

**CS line — same trade-off as before:**

| Option | Wiring | Trade-off |
|---|---|---|
| A. **Tie Pico GP17 LOW** | Pico SPI0 CSn → Pico GND | Slave is always selected. Works because the AMS firmware initialises SPI1 only for LTC (no nRF24 driver in the build), so all SPI1 traffic IS LTC traffic. Frame boundaries are inferred from idle gaps + PEC15 validity. |
| B. **Wire MLC2 PA4 → Pico GP17** | Tap `LTC6820_CS` (STM32 PA4, per firmware `Core/Inc/main.h:68`) → Pico CSn | Proper protocol semantics. PA4 isn't broken out on J8; would need a magnet-wire tap on the MLC2 carrier daughterboard's CS net, then route to Pico. |

Default firmware assumes option A. Option B becomes mandatory if a
future firmware build also enables nRF24 on SPI1 (then we need CS to
disambiguate LTC frames from nRF24 frames).

### Other slots

| Slot | SPI breakout |
|---|---|
| MLC1 | **U22** (full 8-pin nRF24 footprint — easiest if you have a Pico-to-nRF24-pinout adapter) or **J7** patch header (same 4-pin SCK/MISO/MOSI/GND layout as J8) |
| MLC2 | **J8** patch header (this guide) |
| MLC3 | no on-backplane breakout — solder direct to MLC3 connector pads 34/35/36 |
| MLC4 | same as MLC3 |

## Power

Pico VBUS via Pi USB → Pico runs from Pi's 5 V, no separate supply.
Same USB connection is the CDC serial command channel and the
flash-bootloader path.

## Build (Mac or Pi, both work)

Requires Pico SDK ≥ 1.5 and `cmake`/`arm-none-eabi-gcc`.

```sh
cd tools/pico_ltc_emulator/firmware
mkdir -p build && cd build
cmake -DPICO_SDK_PATH=$PICO_SDK_PATH ..
make -j$(nproc)
# -> pico_ltc_emulator.uf2
```

## First flash (one-time, requires button press)

Hold BOOTSEL on the Pico while plugging USB into the Pi. Pico
enumerates as USB MSC. Then on the Pi:

```sh
tools/pico_ltc_emulator/host/flash_pico.sh \
    tools/pico_ltc_emulator/firmware/build/pico_ltc_emulator.uf2
```

## Subsequent flashes (no buttons — controlled from the Pi)

The firmware listens for a `BSL\n` command on its USB-CDC serial.
On receipt it calls `reset_usb_boot()` and re-enumerates as USB MSC.
The flash script does this automatically:

```sh
tools/pico_ltc_emulator/host/flash_pico.sh path/to/new.uf2
```

Internally that runs `picotool reboot -f -u && picotool load -fx
<file.uf2>`, which (a) sends the reboot command over USB if the Pico
is currently in CDC mode, (b) loads the new firmware, (c) lets the
Pico auto-execute.

## Command protocol (USB-CDC, line-oriented, 115200 baud nominal but
USB-CDC is rate-agnostic)

```
PING               -> PONG <fw_version>
STATUS             -> OK n_cmds_rx=<N> n_spi_xact=<N> last_cmd=<HEX>
SET_CELL m c mV    -> OK   (module 0..9, cell 0..11, mV uint16)
SET_TEMP m s dC    -> OK   (module 0..9, sensor 0..5, deci-degC int16)
RESET_STATE        -> OK   (back to seed defaults: 3750 mV, 25 °C)
BSL                -> OK -- bye  (then re-enumerates as MSC)
```

Default state on cold boot: all cells = 3750 mV, all temps = 25 °C —
matches `BmsService::seed_for_hil_stub` so the firmware sees the
same nominal-healthy snapshot.

## Scope of the first cut

This skeleton ships:
- USB-CDC bring-up + line-buffered command parser
- `BSL` reflash path
- SPI slave initialisation on SPI0
- PEC15 (CRC-15) computation matching the LTC6811 datasheet
- Cell/temperature state store, default-seeded
- LTC6811 command decode skeleton (sees the 11-bit command, picks the
  handler, returns deterministic bytes — bytes are PEC15-correct but
  the handler set is incomplete)

What's *not* in the first cut:
- Full LTC6811 command coverage (only WRCFGA / ADCV / RDCVA..D / RDAUXA/B for now)
- Conftest fixture that drives the Pico from a pytest test
- SPI clock-rate validation under load
- Multi-slot support (the firmware assumes one Pico per MLC)

These land in follow-up commits as we wire up and test on real
hardware.
