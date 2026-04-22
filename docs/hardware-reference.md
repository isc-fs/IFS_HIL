# Hardware reference

Signal map for the BACKPLANE_HIL PCB as driven by the Raspberry Pi.
This is the authoritative reference any operator or contributor will
consult when they need to know *"what pin / what address / what
netdev maps to what physical chip."*

The canonical single source of truth in the source tree is
[`tools/hw_config.py`](../tools/hw_config.py); this document is its
expanded companion. If the two disagree, trust `hw_config.py` and
file a PR against this document.

All GPIO numbers are **BCM** numbering throughout.

---

## On-board ICs at a glance

| Ref | Part | Qty | Role | Bus |
|---|---|---:|---|---|
| U9–U11 | MCP3208 | 3 | 8-channel 12-bit ADC | SPI0 (register-level) |
| U12–U15 | DAC80504 | 4 | 4-channel 16-bit DAC | SPI0 (register-level) |
| U17, U19, U21 | MCP2515 | 3 | CAN 2.0B controller | SPI0 (kernel `mcp251x`) |
| U16, U18, U20 | SN65HVD230 | 3 | CAN transceiver | paired with each MCP2515 |
| U1, U2, U4 (+ standby) | INA226 | 4 | Power monitor per MLC carrier | I²C1 |
| U3, U6, U8 | TCA9555 | 3 | 16-bit GPIO expander | I²C1 |
| U5 | TLV75533 | 1 | LDO: +5VSBY → +3V3SBY | — |
| IC1 | SN74LVC125A | 1 | Quad tri-state buffer on MOSI/MISO/SCK | enabled by Q5 via `PWR_OK` |
| U23 | nRF24L01+ | 1 | 2.4 GHz transceiver (not populated on current boards) | SPI0 (register-level) |
| Q1–Q4 | DMN6075S | 4 | Relay driver NMOS for K1–K4 | TCA9555 port 0 bits 0–3 |
| Q5 | DMN6075S | 1 | Enables IC1 `~OE` from `PWR_OK` | GPIO8 (PWR_OK) |
| K1–K4 | RT314A12 | 4 | +12 V SPDT relay per MLC carrier | driven by Q1–Q4 |
| Y1–Y3 | 16 MHz crystal | 3 | MCP2515 clock source | — |

---

## SPI0 bus

Single hardware SPI0 master; three chip-select "identities":

- **Kernel `mcp251x` driver** owns `spi0.0`, `spi0.1`, `spi0.2` via the
  `mcp2515-triple` device-tree overlay. Each SPI device maps to one
  MCP2515, and each MCP2515 appears to userspace as a SocketCAN
  netdev (`canN`). See the [CAN netdev ↔ PCB label
  mapping](#can-netdev--pcb-label-mapping-crucial) section below.
- **`/dev/spidev0.3`** is a userspace spidev node the broker uses for
  the non-CAN SPI chips. It is configured with `no_cs=True` so the
  hardware never drives any CS during transfers — the broker pulses
  the real CS GPIOs manually, one at a time. This avoids any
  interaction with the kernel's `cs-gpios` mechanism, which would
  otherwise try to assert an extra CS around every transaction.

### SPI physical pins

| BCM GPIO | Header pin | Function |
|---:|---:|---|
| 9  | 21 | SPI0 MISO |
| 10 | 19 | SPI0 MOSI |
| 11 | 23 | SPI0 SCLK |

All three data lines pass through IC1 (`SN74LVC125A`) before reaching
the on-board ICs. IC1's `~OE` is pulled low by Q5 only when ATX
`PWR_OK` is high — that is, the SPI bus to the peripherals is
**gated by the PSU being alive**.

### SPI chip-selects (all active-low, software-driven)

| BCM GPIO | Header pin | CS for | Managed by |
|---:|---:|---|---|
| 27 | 13 | MCP2515 U17 (PCB **CAN1**) | kernel `mcp251x` (via `cs-gpios`) |
| 17 | 11 | MCP2515 U19 (PCB **CAN2**) | kernel `mcp251x` (via `cs-gpios`) |
| 18 | 12 | MCP2515 U21 (PCB **CAN3**) | kernel `mcp251x` (via `cs-gpios`) |
| 16 | 36 | **unused** (dummy CS for `spidev0.3`) | – |
| 19 | 35 | MCP3208 U9  (ADC1) | broker, `RPi.GPIO.output()` |
| 20 | 38 | MCP3208 U10 (ADC2) | broker |
| 21 | 40 | MCP3208 U11 (ADC3) | broker |
| 22 | 15 | DAC80504 U12 (DAC1) | broker |
| 23 | 16 | DAC80504 U13 (DAC2) | broker |
| 24 | 18 | DAC80504 U14 (DAC3) | broker |
| 25 | 22 | DAC80504 U15 (DAC4) | broker |
| 26 | 37 | nRF24L01+ (U23, unpopulated) | broker |

---

## MCP2515 CAN controllers

### CAN netdev ↔ PCB label mapping (CRUCIAL)

The kernel `mcp251x` driver on this kernel probes SPI children in
**reverse `reg` order**, so the kernel netdev names are **inverted**
relative to the PCB silk-screen labels. Forgetting this fact is the
single most common reason `can-flasher discover` returns empty.

| kernel netdev | spi device | CS GPIO | PCB label | chip | INT GPIO |
|---|---|---:|---|---|---:|
| `can0` | `spi0.2` | 18 | **CAN3** | U21 | 6 |
| `can1` | `spi0.1` | 17 | **CAN2** | U19 | 5 |
| `can2` | `spi0.0` | 27 | **CAN1** | U17 | 4 |

The MLC1..MLC4 carrier boards are wired to PCB CAN1 — which is kernel
**`can2`**. Flashing always targets `can2`.

### MCP2515 crystals

Each MCP2515 has its own 16 MHz crystal (Y1/Y2/Y3) with 22 pF load
caps. The CAN bitrate calculation happens in the kernel driver based
on the device-tree `clocks` reference to a `fixed-clock` node at 16
MHz; see [`infra/devicetree/mcp2515-triple.dts`](../infra/devicetree/mcp2515-triple.dts).

Bus bitrate for all ECU traffic is **500 kbps**.

### Auto-recovery

`hil-can-up.service` brings each interface up with
`restart-ms=200`, so the kernel auto-recovers from bus-off after
200 ms without operator intervention.

---

## I²C bus (`/dev/i2c-1`)

| BCM GPIO | Header pin | Function |
|---:|---:|---|
| 2 | 3 | I²C1 SDA |
| 3 | 5 | I²C1 SCL |

Pull-ups `R50`/`R51` are to `+3V3_SBY`; the I²C bus stays alive
whether the main ATX rails are on or not.

### INA226 power monitors (one per MLC carrier)

Low-side current-sensing wiring. `bus_voltage()` reads ~0 V by design;
only `current()` and `power()` carry useful information.

| Address | Carrier | A1 | A0 |
|---:|---|---|---|
| `0x40` | **MLC1** | GND | GND |
| `0x41` | **MLC2** | GND | VS  |
| `0x44` | **MLC3** | VS  | GND |
| `0x45` | **MLC4** | VS  | VS  |

Shunt resistor: **10 mΩ** (`INA226_SHUNT_OHM = 0.01`).
Overcurrent warning threshold in the broker: **3 A**
(`MLC_CURRENT_MAX_A = 3.0`).

Typical MLC current with an STM32H733 bootloader idling on the
carrier: **~130 mA**. ≤ 1 mA means the relay didn't close or the
fuse is blown.

### TCA9555 I/O expanders

| Address | Ref | Role |
|---:|---|---|
| `0x20` | **U3** | Relay coil drivers (port 0 bits 0–3) |
| `0x21` | **U6** | Slot LED indicators (port 1 bit 4 = SLOT3_LED_RESULT) |
| `0x22` | **U8** | Other I/O |

Each TCA9555 has 16 bidirectional pins split into two 8-bit ports.
By default after power-on, all pins are inputs with the chip's
internal weak pull-up. The broker configures pins as outputs before
driving them (`tca.set_direction`).

### Carrier relay map

Relay K_n energises MLC_n. Each relay's NMOS gate driver is wired to
TCA9555 **U3** (addr `0x20`) **port 0**:

| Relay | TCA9555 addr | Port | Bit | MLC carrier |
|---|---:|---:|---:|---|
| K1 | `0x20` | 0 | 0 | MLC1 |
| K2 | `0x20` | 0 | 1 | MLC2 |
| K3 | `0x20` | 0 | 2 | MLC3 |
| K4 | `0x20` | 0 | 3 | MLC4 |

To energise one carrier:

```python
c.call('tca.set_direction', addr=0x20, port=0, mask=0x00)  # port0 all outputs
c.call('tca.write_pin',    addr=0x20, port=0, pin=0, value=True)  # K1 on
```

Or from the dashboard's "Carrier N power" toggle.

---

## Power control signals

| BCM GPIO | Header pin | Signal | Direction | Polarity |
|---:|---:|---|---|---|
| 7 | 26 | `PS_ON#` → ATX | output | active-LOW (drive low = PSU on) |
| 8 | 24 | `PWR_OK` ← ATX | input | active-HIGH (high = rails stable) |

### Boot-time assertion

The Pi 4's GPIO output-state persists across reboots, so a prior
userspace `pinctrl set 7 op dh` (or a broker crash mid-shutdown) can
stick and leave `PS_ON#` de-asserted. Two safeguards land the correct
state anyway:

1. **Firmware directive** in `/boot/firmware/config.txt`:
   ```
   gpio=7=op,dl
   gpio=8=ip,pd
   ```
   Runs before the kernel boots, so `mcp251x` probe sees powered chips.

2. **`hil-psu-on.service`** re-asserts the same state in userspace at
   `sysinit.target`, as a belt-and-braces against the firmware
   directive being defeated by the persistence quirk.

### MISO buffer gating (hardware-only)

IC1 (`SN74LVC125A`) buffers MOSI, MISO, and SCK. All four `~OE` pins
are tied to Q5 (NMOS, gate = `PWR_OK`, source = GND, drain = `~OE`).

- `PWR_OK = HIGH`  → Q5 conducts → `~OE = LOW` → buffer **enabled**.
- `PWR_OK = LOW`   → Q5 off        → `~OE` floats → buffer disabled.

**The SPI bus to every peripheral is therefore silent when the PSU
is off.** This is why `psu.power(True)` is a prerequisite for
anything via SPI, including reading MCP3208s.

---

## Pi header pinout reference

For clarity, all GPIOs this bench uses by BCM number, sorted:

| BCM | Pi header | Role |
|---:|---:|---|
| 2  | 3  | I²C1 SDA |
| 3  | 5  | I²C1 SCL |
| 4  | 7  | INT from MCP2515 U17 (PCB CAN1 → kernel `can2`) |
| 5  | 29 | INT from MCP2515 U19 (PCB CAN2 → kernel `can1`) |
| 6  | 31 | INT from MCP2515 U21 (PCB CAN3 → kernel `can0`) |
| 7  | 26 | `PS_ON#` to ATX |
| 8  | 24 | `PWR_OK` from ATX |
| 9  | 21 | SPI0 MISO |
| 10 | 19 | SPI0 MOSI |
| 11 | 23 | SPI0 SCLK |
| 12 | 32 | nRF24 IRQ (unpopulated) |
| 13 | 33 | nRF24 CE (unpopulated) |
| 16 | 36 | dummy CS for `spidev0.3` |
| 17 | 11 | CS for MCP2515 U19 |
| 18 | 12 | CS for MCP2515 U21 |
| 19 | 35 | CS for MCP3208 U9 |
| 20 | 38 | CS for MCP3208 U10 |
| 21 | 40 | CS for MCP3208 U11 |
| 22 | 15 | CS for DAC80504 U12 |
| 23 | 16 | CS for DAC80504 U13 |
| 24 | 18 | CS for DAC80504 U14 |
| 25 | 22 | CS for DAC80504 U15 |
| 26 | 37 | CS for nRF24 (unpopulated) |
| 27 | 13 | CS for MCP2515 U17 |

---

## Power tree

Abridged from the PCB design review:

```
ATX PSU (J1)
├── +12V ──── Relay coils K1–K4 (via Q1–Q4 / F1–F4)
├── +5V  ──── CAN transceiver, buffer, general 5 V rail
├── +5V_SBY ─┬── U5 LDO ── +3V3_SBY ── INA226 ×4, TCA9555 ×3
│            └── Pi J2 pin 2/4 ── RPi input power
│                                 (⚠ must sustain 3 A peak)
└── +3V3 (ATX) ─ MCP3208 ×3, DAC80504 ×4 (+VREF), MCP2515 ×3, SN65HVD230 ×3

RPi 3V3_out (J2 pin 1) ── IC1 (SN74LVC125A) ── buffered SPI to peripherals
```

Important consequences:

- **I²C peripherals (INA226, TCA9555) are on standby power.** They
  answer even when the PSU is off. Useful for pre-flight checks.
- **The MCP2515s and the rest of SPI are on main 3V3**, so they need
  `PS_ON#` asserted before kernel `mcp251x` probe will succeed.
- **The Pi is powered from 5VSBY.** A supply rated below ~3 A on SBY
  produces undervoltage events during kernel boot and SPI bursts —
  which were the root cause of several false-positive mcp251x
  diagnostics during bring-up. Use a PSU with ≥ 3 A on 5VSBY.

---

## Where the single source of truth lives

- **Pin assignments, I²C addresses, constants** →
  [`tools/hw_config.py`](../tools/hw_config.py)
- **Device-tree overlay** (SPI bus layout for the kernel) →
  [`infra/devicetree/mcp2515-triple.dts`](../infra/devicetree/mcp2515-triple.dts)
- **Kernel module (patched)** →
  [`infra/kernel-module/mcp251x-patched/`](../infra/kernel-module/mcp251x-patched/)
- **This document** — human-readable companion; PRs welcome when
  something changes.
