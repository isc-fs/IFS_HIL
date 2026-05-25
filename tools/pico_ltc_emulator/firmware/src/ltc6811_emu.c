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

// ltc6811_emu_response_for() is defined further down, after the
// response_pool[] static is in scope.

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

// Each "response" is 4 bytes of pad (master ignores during its cmd
// phase) + 80 bytes of LTC chain data (10 chips * 8 bytes). The pad
// realigns our streamed buffer to where the master expects the slave
// to start replying.
#define RESPONSE_PAD 4
#define RESPONSE_LEN (RESPONSE_PAD + 80)

// 6 pre-built response buffers indexed by cmd: RDCVA/B/C/D + RDAUXA/B.
// Computed at init (and on every cell/temp change via the USB cmds).
// On cmd parse we just swap which buffer the streamer reads from --
// fast enough to fit in the ~8 us between master-byte-4 and byte-5.
enum { RSP_RDCVA, RSP_RDCVB, RSP_RDCVC, RSP_RDCVD, RSP_RDAUXA, RSP_RDAUXB, RSP_COUNT };
static uint8_t response_pool[RSP_COUNT][RESPONSE_LEN];
static const uint8_t *current_response = response_pool[RSP_RDCVA];  // default

// TX-snapshot diagnostic. Records every byte we push to TX FIFO during
// a single transaction so the host can verify the streamer's output
// matches the intended response_pool. Captured into `tx_snap` until
// either the buffer fills (LTC_RESPONSE_LEN) or CS rises. On CS-rising
// the working snapshot is frozen into `tx_snap_published` and the next
// xact starts capturing fresh into `tx_snap` again.
static uint8_t  tx_snap[RESPONSE_LEN];
static uint16_t tx_snap_idx          = 0;
static uint16_t tx_snap_cmd_recorded = 0xFFFFu;
static uint8_t  tx_snap_published[RESPONSE_LEN];
static uint16_t tx_snap_published_len = 0;
static uint16_t tx_snap_published_cmd = 0xFFFFu;

static void build_one_response(int slot, int is_rdcv, int is_rdaux, uint8_t group) {
    uint8_t *buf = response_pool[slot];
    // 4 bytes of pad first (don't-care during master's cmd phase)
    for (int i = 0; i < RESPONSE_PAD; i++) buf[i] = 0xFFu;
    // Then 10 * 8 bytes of chain data + PEC15 per chip
    uint8_t chunk[8];
    for (uint8_t ltc = 0; ltc < LTC_CHAIN_LEN; ltc++) {
        if (is_rdcv) {
            build_rdcv_chunk(ltc, group, chunk);
        } else if (is_rdaux) {
            build_rdaux_chunk(ltc, group, chunk);
        } else {
            for (int i = 0; i < 8; i++) chunk[i] = 0xFFu;
        }
        for (int i = 0; i < 8; i++) buf[RESPONSE_PAD + ltc * 8 + i] = chunk[i];
    }
}

static void rebuild_all_responses(void) {
    build_one_response(RSP_RDCVA,  1, 0, 0);
    build_one_response(RSP_RDCVB,  1, 0, 1);
    build_one_response(RSP_RDCVC,  1, 0, 2);
    build_one_response(RSP_RDCVD,  1, 0, 3);
    build_one_response(RSP_RDAUXA, 0, 1, 0);
    build_one_response(RSP_RDAUXB, 0, 1, 1);
}

void ltc6811_emu_refresh_responses(void) {
    rebuild_all_responses();
}

const uint8_t *ltc6811_emu_response_for(uint16_t cmd) {
    switch (cmd) {
        case 0x004: return response_pool[RSP_RDCVA];
        case 0x006: return response_pool[RSP_RDCVB];
        case 0x008: return response_pool[RSP_RDCVC];
        case 0x00A: return response_pool[RSP_RDCVD];
        case 0x00C: return response_pool[RSP_RDAUXA];
        case 0x00E: return response_pool[RSP_RDAUXB];
        default:    return NULL;
    }
}

const uint8_t *ltc6811_emu_last_tx_snapshot(uint16_t *out_cmd, uint16_t *out_len) {
    if (out_cmd) *out_cmd = tx_snap_published_cmd;
    if (out_len) *out_len = tx_snap_published_len;
    return tx_snap_published;
}

// Push one byte to TX FIFO and record it in the per-xact snapshot
// buffer (truncating once full). Re-verifies writability immediately
// before writing -- the PL022 TNF flag can lag the actual FIFO state
// by a cycle or two after a previous write, so a caller-side check
// followed by a second push can silently drop. Returns 1 if the byte
// went in, 0 if FIFO was full.
static inline int tx_push(uint8_t b) {
    if (!spi_is_writable(PICO_SPI)) return 0;
    spi_get_hw(PICO_SPI)->dr = b;
    if (tx_snap_idx < RESPONSE_LEN) {
        tx_snap[tx_snap_idx++] = b;
    }
    return 1;
}

// Streamer design (v0.1.5):
//
// The PL022 TX FIFO is 8 bytes deep. The master's cmd phase is exactly
// 4 bytes (a daisy-chain command frame). If we pre-load more than 4
// bytes at CS-fall, the extras spill into the data phase and the
// master receives them in place of real cell data -- so the first
// chip's PEC check fails.
//
// The fix: at CS-fall pre-load EXACTLY RESPONSE_PAD (=4) pad bytes
// into the TX FIFO. While those are clocking out (master's cmd phase),
// we have time to parse the inbound cmd and pick the right response
// buffer. After cmd parse we start streaming data bytes from
// current_response[RESPONSE_PAD..] into the FIFO. The master sees:
//   bytes 0..3  = pad (ignored, cmd phase)
//   bytes 4..83 = data from the buffer matching the parsed cmd
//
// Timing budget: at 1 MHz SCK, one master byte takes 8 µs. The cmd
// phase is 32 µs. STM32H7 also inserts a small CPU gap between
// HAL_SPI_Transmit (the 4-byte cmd send) and HAL_SPI_TransmitReceive
// (the 80-byte read), giving us extra headroom. Our service loop
// runs every ~1 µs (no blocking calls in main.c) so cmd parse easily
// completes before the master starts byte 4.
void ltc6811_emu_service(void) {
    static uint8_t  rx_buf[4];
    static uint8_t  rx_idx              = 0;
    static int      tx_data_idx         = 0;   // index into current_response[RESPONSE_PAD..]
    static int      cmd_parsed_this_xact = 0;
    static int      cs_fall_setup_done   = 0;
    static int      cs_was_low           = 0;
    static int      response_init_done   = 0;

    if (!response_init_done) {
        rebuild_all_responses();
        current_response = response_pool[RSP_RDCVA];
        response_init_done = 1;
    }

    int cs_now = gpio_get(PIN_SPI_CSN);

    if (cs_was_low && cs_now) {
        // CS rising edge: transaction ended. Freeze the per-xact TX
        // snapshot first (host reads via DUMP_TX), then cycle SSE to
        // flush both FIFOs so the NEXT xact starts from a clean
        // slate. SCK is idle right now (master deasserted CS), so the
        // brief PL022 disable is harmless.
        tx_snap_published_len = tx_snap_idx;
        tx_snap_published_cmd = tx_snap_cmd_recorded;
        for (uint16_t i = 0; i < tx_snap_idx; i++) tx_snap_published[i] = tx_snap[i];
        tx_snap_idx          = 0;
        tx_snap_cmd_recorded = 0xFFFFu;

        spi_get_hw(PICO_SPI)->cr1 &= ~SPI_SSPCR1_SSE_BITS;
        spi_get_hw(PICO_SPI)->cr1 |=  SPI_SSPCR1_SSE_BITS;
        rx_idx               = 0;
        tx_data_idx          = 0;
        cmd_parsed_this_xact = 0;
        cs_fall_setup_done   = 0;
        g_ltc_stats.n_cs_cycles++;
    }
    cs_was_low = !cs_now;

    if (!cs_now) {
        // Active transaction.
        if (!cs_fall_setup_done) {
            // First service call after CS-fall: push EXACTLY 4 pad
            // bytes into the TX FIFO (covers master bytes 0..3 = cmd
            // phase). Do NOT push more -- the rest of the FIFO must
            // stay free for the post-cmd-parse data stream so the
            // first data byte actually comes from current_response,
            // not from a stale pad.
            for (int i = 0; i < RESPONSE_PAD; i++) {
                while (!spi_is_writable(PICO_SPI)) { /* spin */ }
                tx_push(0xFFu);
            }
            cs_fall_setup_done = 1;
        }

        if (cmd_parsed_this_xact) {
            // Cmd parsed: pump bytes into the TX FIFO. One loop, one
            // gate -- branch inside picks data vs pad. Two separate
            // loops would race the PL022 TNF flag (lazy update after
            // a write) and let a stray pad byte slip into the data
            // stream every few FIFO refills, corrupting the response.
            while (spi_is_writable(PICO_SPI)) {
                uint8_t b = (tx_data_idx < (RESPONSE_LEN - RESPONSE_PAD))
                                ? current_response[RESPONSE_PAD + tx_data_idx]
                                : 0xFFu;
                if (!tx_push(b)) break;          // FIFO truly full
                if (tx_data_idx < (RESPONSE_LEN - RESPONSE_PAD)) tx_data_idx++;
            }
        }
    }

    // RX side: drain bytes, parse cmd every 4 bytes, update response
    // buffer pointer.
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
        g_ltc_stats.last_cmd = cmd;

        // Whitelist of LTC6811 opcodes we care about. Anything outside
        // this set is most likely a data-byte chunk the master clocks
        // during a read or write phase (e.g. all 0xFF -> cmd=0x7FF).
        // Tracking these separately tells us if real LTC commands are
        // reaching the slave. We only swap current_response on the
        // FIRST parsed cmd of each xact -- subsequent 4-byte "chunks"
        // are data, not cmds, and must not retarget the streamer.
        if (!cmd_parsed_this_xact) {
            int is_known = 0;
            switch (cmd) {
                case 0x001:  // WRCFGA
                case 0x002:  // RDCFGA
                case 0x010:  // RDSTATA
                case 0x012:  // RDSTATB
                case 0x014:  // WRSCTRL
                case 0x016:  // WRPWM
                case 0x024:  // WRCFGB
                case 0x026:  // RDCFGB
                case 0x02C:  // RDSID
                case 0x714:  // PLADC
                case 0x721:  // WRCOMM
                case 0x722:  // RDCOMM
                case 0x723:  // STCOMM
                    is_known = 1; break;
                case 0x004:  // RDCVA
                    is_known = 1; current_response = response_pool[RSP_RDCVA];  break;
                case 0x006:  // RDCVB
                    is_known = 1; current_response = response_pool[RSP_RDCVB];  break;
                case 0x008:  // RDCVC
                    is_known = 1; current_response = response_pool[RSP_RDCVC];  break;
                case 0x00A:  // RDCVD
                    is_known = 1; current_response = response_pool[RSP_RDCVD];  break;
                case 0x00C:  // RDAUXA
                    is_known = 1; current_response = response_pool[RSP_RDAUXA]; break;
                case 0x00E:  // RDAUXB
                    is_known = 1; current_response = response_pool[RSP_RDAUXB]; break;
                default:
                    // ADCV variants: 0x260..0x27F, 0x340..0x37F
                    if ((cmd & 0x7C0u) == 0x260u || (cmd & 0x7C0u) == 0x340u) is_known = 1;
                    // ADAX variants: 0x460..0x47F, 0x540..0x57F
                    if ((cmd & 0x7C0u) == 0x460u || (cmd & 0x7C0u) == 0x540u) is_known = 1;
                    break;
            }
            if (is_known) {
                g_ltc_stats.last_ltc_cmd = cmd;
                g_ltc_stats.n_valid_cmds++;
            }
            tx_snap_cmd_recorded = cmd;
            cmd_parsed_this_xact = 1;
        }
    }
}
