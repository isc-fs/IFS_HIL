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

## Wiring (target: MLC1 slot)

The MLC1 carrier already exposes its STM32 SPI1 bus on the **U22 nRF24
footprint** — same pins the firmware would talk to an LTC6820 on. Pop
the nRF24 module out of U22 (if installed) and wire the Pico into the
freed footprint:

| Pico GPIO | U22 pad | Net | Notes |
|---|---|---|---|
| GP18 (SPI0 SCK)  | pad 5 (SCK)  | `/SLOT1_SPI_SCK`  | clock from MLC1 master |
| GP19 (SPI0 TX)   | pad 7 (MISO) | `/SLOT1_SPI_MISO` | **slave drives MISO** (Pico→MLC) |
| GP16 (SPI0 RX)   | pad 6 (MOSI) | `/SLOT1_SPI_MOSI` | MLC→Pico, master output |
| any Pico GND     | pad 1 (GND)  | GND               |  |
| any Pico VBUS/3V3| pad 2 (VCC)  | `3V3_ECU`         | optional — Pico is also USB-powered, don't double-feed |

**CS line — open question, two paths:**

| Option | Wiring | Trade-off |
|---|---|---|
| A. **Tie Pico GP17 LOW** | Pico SPI0 CSn → GND | Slave is always selected. Works because the AMS firmware initialises SPI1 only for LTC (no nRF24 driver in the build), so all SPI1 traffic IS LTC traffic. Frame boundaries are inferred from `kSafetyPeriodMs` idle gaps + PEC15 validity. |
| B. **Wire MLC1 PA4 → Pico GP17** | Tap `LTC6820_CS` (STM32 PA4) → Pico CSn | Proper protocol semantics. Requires identifying which BACKPLANE pad carries MLC1's PA4 — it's NOT on U22, NOT on the carrier connector under a named net, likely needs a magnet-wire tap on the daughterboard. |

Default firmware assumes option A. Option B becomes mandatory if a
future firmware build also enables nRF24 on SPI1 (then we need CS to
disambiguate LTC frames from nRF24 frames).

### Why MLC1 specifically

The nRF24 footprint exposing the SPI bus only exists on **MLC1**
(`U22`); MLC2/3/4 have the SPI on the carrier connector pads 34/35/36
but no on-backplane breakout. For MLC1, U22 is the cleanest hookup.
For the other slots you'd fall back to soldering to the carrier
connector or to the J7/J8 patch headers (J7 for SLOT1, J8 for SLOT2).

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
