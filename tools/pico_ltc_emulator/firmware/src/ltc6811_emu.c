// LTC6811 chain emulator. First-cut skeleton.
//
// What it handles:
//   WRCFGA (0x001)   accept 6*N config bytes, no-op (we don't honour
//                    discharge bits)
//   ADCV   (variants 0x260..0x370)  noop -- conversions are "always done"
//   RDCVA  (0x004)   return cells 0..2 for each chip, PEC15 per chip
//   RDCVB  (0x006)   cells 3..5
//   RDCVC  (0x008)   cells 6..8
//   RDCVD  (0x00A)   cells 9..11
//   RDAUXA (0x00C)   aux 0..2 (NTC voltages computed from temp_dC)
//   RDAUXB (0x00E)   aux 3..4 + ref2 stub
//
// Unknown commands return all-0xFF with a PEC trailer; the firmware
// PEC15 check fails per-chip and the IC is treated as offline. That
// surfaces as a clean fault rather than corrupted data.

#include <string.h>

#include "ltc6811_emu.h"
#include "cell_state.h"
#include "pec15.h"

#include "hardware/spi.h"
#include "hardware/gpio.h"
#include "pico/stdlib.h"

#define PICO_SPI            spi0
#define PIN_SPI_RX          16   // MOSI from master
#define PIN_SPI_CSN         17   // CS from master
#define PIN_SPI_SCK         18
#define PIN_SPI_TX          19   // MISO to master

// LTC6811 cell voltage encoding: each cell is uint16_t in units of
// 100 microvolts (0.1 mV). cell_mV * 10 gives the on-wire value.
static inline uint16_t mV_to_ltc(uint16_t mV) {
    // Saturate to uint16 max (≈ 6.55 V) -- way above any sane cell.
    uint32_t v = (uint32_t)mV * 10u;
    return (v > 0xFFFFu) ? 0xFFFFu : (uint16_t)v;
}

// Temperature-to-NTC-divider voltage: stand-in scaling for the first
// cut. AMS firmware reads aux as Volt-scaled u16 (units 100 µV like
// cells) and applies its own NTC table. We pick a placeholder linear
// mapping: 25 °C -> 1.5 V (15000 in u16 100µV units). Tune later.
static inline uint16_t dC_to_ltc(int16_t dC) {
    int32_t centred = 1500 - (dC - 250);  // 25 °C -> 1500, -5 mV per °C
    if (centred < 0)       centred = 0;
    if (centred > 0xFFFF)  centred = 0xFFFF;
    return (uint16_t)centred;
}

ltc_stats_t g_ltc_stats;

void ltc6811_emu_reset_stats(void) {
    g_ltc_stats.n_spi_xact = 0;
    g_ltc_stats.last_cmd   = 0;
}

void ltc6811_emu_init(void) {
    spi_init(PICO_SPI, 1000 * 1000);          // 1 MHz to match LTC6820 typical
    spi_set_slave(PICO_SPI, true);
    // SPI mode 3 (CPOL=1, CPHA=1) -- this is what the AMS firmware
    // configures SPI1 with (CLKPolarity=HIGH, CLKPhase=2EDGE in
    // main.c:573-574). Mode 3 is also the LTC6820 native protocol.
    // Mode 0 (which we had initially) leaves the Pico's slave
    // expecting SCK idle low + rising-edge sample, while the master
    // is driving SCK idle high + falling-edge transition + rising-edge
    // sample. Pico ends up sampling garbage and n_spi_xact stays at 0.
    spi_set_format(PICO_SPI, 8, SPI_CPOL_1, SPI_CPHA_1, SPI_MSB_FIRST);

    // Enable internal pull-downs on SCK + MOSI BEFORE switching to SPI
    // alternate function. While the MLC STM32 is in BL mode, its SPI1
    // pins (PA5/6/7) sit in HiZ reset state -- a long wire between J8
    // and the Pico will float and pick up enough noise to clock the
    // Pico's SPI slave, which then drives MISO with garbage and
    // disrupts the BL's CAN-flash timing. Pulling these down at the
    // Pico end forces a clean low when nothing is driving the wire.
    // The MLC's SPI peripheral easily overrides the ~50 kOhm internal
    // pull-down during real transfers (typical SPI drive strength
    // is single-digit ohms).
    gpio_pull_down(PIN_SPI_SCK);
    gpio_pull_down(PIN_SPI_RX);

    gpio_set_function(PIN_SPI_RX,  GPIO_FUNC_SPI);
    gpio_set_function(PIN_SPI_TX,  GPIO_FUNC_SPI);
    gpio_set_function(PIN_SPI_SCK, GPIO_FUNC_SPI);
    gpio_set_function(PIN_SPI_CSN, GPIO_FUNC_SPI);

    ltc6811_emu_reset_stats();
}

// Build the 8-byte per-chip response for RDCV* group `group` (0..3):
//   bytes 0..1  cell[group*3 + 0]   (little-endian)
//   bytes 2..3  cell[group*3 + 1]
//   bytes 4..5  cell[group*3 + 2]
//   bytes 6..7  PEC15(bytes 0..5)
static void build_rdcv_chunk(uint8_t ltc_idx, uint8_t group, uint8_t out[8]) {
    for (uint8_t i = 0; i < 3; i++) {
        uint8_t cell = group * 3 + i;
        uint16_t v = mV_to_ltc(g_state.cell_mV[ltc_idx][cell]);
        out[i * 2]     = (uint8_t)(v & 0xFFu);
        out[i * 2 + 1] = (uint8_t)((v >> 8) & 0xFFu);
    }
    pec15_append(out, 6);
}

// Same idea for RDAUXA (group 0: aux 0..2) and RDAUXB (group 1:
// aux 3..4 + a stub ref2 = 30000).
static void build_rdaux_chunk(uint8_t ltc_idx, uint8_t group, uint8_t out[8]) {
    if (group == 0) {
        for (uint8_t i = 0; i < 3; i++) {
            uint16_t v = dC_to_ltc(g_state.temp_dC[ltc_idx][i]);
            out[i * 2]     = (uint8_t)(v & 0xFFu);
            out[i * 2 + 1] = (uint8_t)((v >> 8) & 0xFFu);
        }
    } else {
        uint16_t v;
        v = dC_to_ltc(g_state.temp_dC[ltc_idx][3]);
        out[0] = (uint8_t)(v & 0xFFu);   out[1] = (uint8_t)((v >> 8) & 0xFFu);
        v = dC_to_ltc(g_state.temp_dC[ltc_idx][4]);
        out[2] = (uint8_t)(v & 0xFFu);   out[3] = (uint8_t)((v >> 8) & 0xFFu);
        // Stub ref2 reading: 30000 (= 3.0 V in 100 µV units)
        out[4] = 0x30; out[5] = 0x75;
    }
    pec15_append(out, 6);
}

// Continuous TX streaming + RX command parsing.
//
// Why streaming: the AMS firmware uses HAL_SPI_TransmitReceive per
// ltc6820.cpp:113 -- one CS pulse wraps a 4-byte cmd + N data bytes
// clocked back-to-back. The slave must drive MISO with response bytes
// concurrently with the master clocking them. A polled "wait for cmd,
// then write response" strategy is always 80 bytes too late.
//
// Strategy: keep TX FIFO always full with bytes from a 80-byte
// "current response" buffer (10 chips * 8 bytes for RDCVx / RDAUXx).
// The master gets valid data immediately, regardless of the cmd-parse
// latency. When we DO parse a new cmd, swap the response buffer
// contents -- the next time master clocks, it gets the right group.
//
// First-byte fidelity: at init, we pre-build the RDCVA group 0 response
// (cells 1..3). Most LTC chain init sequences start with WRCFGA + ADCV
// + RDCVA, so the first read the firmware does will already have the
// right data in TX FIFO. Subsequent RDCVB/C/D get the correct response
// on the NEXT poll cycle after cmd parsing catches up.

#define RESPONSE_LEN 80   // 10 chips * 8 bytes
static uint8_t current_response[RESPONSE_LEN];

static void build_response(int is_rdcv, int is_rdaux, uint8_t group) {
    uint8_t chunk[8];
    for (uint8_t ltc = 0; ltc < LTC_CHAIN_LEN; ltc++) {
        if (is_rdcv) {
            build_rdcv_chunk(ltc, group, chunk);
        } else if (is_rdaux) {
            build_rdaux_chunk(ltc, group, chunk);
        } else {
            // Idle filler: all 0xFF (LTC chain dormant)
            for (int i = 0; i < 8; i++) chunk[i] = 0xFFu;
        }
        for (int i = 0; i < 8; i++) {
            current_response[ltc * 8 + i] = chunk[i];
        }
    }
}

void ltc6811_emu_service(void) {
    static uint8_t  rx_buf[4];
    static uint8_t  rx_idx     = 0;
    static int      tx_idx     = 0;
    static int      response_init_done = 0;

    if (!response_init_done) {
        // Pre-build RDCVA group 0 as the default streamed response.
        build_response(1, 0, 0);
        // Pre-fill TX FIFO so the very first byte master clocks is
        // valid (not stale 0x00 from peripheral reset).
        while (spi_is_writable(PICO_SPI) && tx_idx < RESPONSE_LEN) {
            spi_get_hw(PICO_SPI)->dr = current_response[tx_idx++];
        }
        tx_idx %= RESPONSE_LEN;
        response_init_done = 1;
    }

    // TX side: keep TX FIFO full, cycling through current_response.
    // The master might be in the middle of a read; we want bytes
    // available now, not after we parse the next cmd.
    while (spi_is_writable(PICO_SPI)) {
        spi_get_hw(PICO_SPI)->dr = current_response[tx_idx];
        tx_idx = (tx_idx + 1) % RESPONSE_LEN;
    }

    // RX side: drain bytes, parse cmd every 4 bytes, update response.
    while (spi_is_readable(PICO_SPI)) {
        uint8_t b = (uint8_t)spi_get_hw(PICO_SPI)->dr;
        // Debug: keep a sliding window of the last 8 raw bytes
        g_ltc_stats.last_rx[g_ltc_stats.rx_byte_count % 8u] = b;
        g_ltc_stats.rx_byte_count++;
        rx_buf[rx_idx++] = b;
        if (rx_idx < 4) continue;
        rx_idx = 0;

        // 11-bit command in rx_buf[0..1].
        uint16_t cmd = (uint16_t)(((rx_buf[0] & 0x07u) << 8) | rx_buf[1]);
        g_ltc_stats.n_spi_xact++;
        // Only record non-zero cmds in last_cmd -- the "data" bytes
        // the master clocks out during a read phase are typically 0x00
        // and would otherwise immediately overwrite the real cmd.
        if (cmd != 0) g_ltc_stats.last_cmd = cmd;

        uint8_t group = 0xFFu;
        int     is_rdcv = 0;
        int     is_rdaux = 0;

        switch (cmd) {
            case 0x004: is_rdcv = 1; group = 0; break;     // RDCVA
            case 0x006: is_rdcv = 1; group = 1; break;     // RDCVB
            case 0x008: is_rdcv = 1; group = 2; break;     // RDCVC
            case 0x00A: is_rdcv = 1; group = 3; break;     // RDCVD
            case 0x00C: is_rdaux = 1; group = 0; break;    // RDAUXA
            case 0x00E: is_rdaux = 1; group = 1; break;    // RDAUXB
            default:
                // WRCFGA / ADCV / etc. -- nothing for us to drive,
                // but keep streaming current_response so TX FIFO
                // doesn't underflow during the master's clocking.
                continue;
        }

        // Build the response for the requested group. The master is
        // probably already clocking bytes out of the buffer right now;
        // they'll get the OLD response for this poll, then the NEW
        // response from the next poll cycle. Acceptable for a
        // constant-cell-V emulation where successive polls of the same
        // group return identical bytes anyway.
        build_response(is_rdcv, is_rdaux, group);
    }
}
