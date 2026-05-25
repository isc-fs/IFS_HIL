// LTC6811 chain emulator.
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
//
// SPI plumbing: PIO instead of PL022. Two state machines on PIO0:
//   - sm_tx (spi_slave_tx program): shifts MISO out on every SCK
//     falling edge while CSn is LOW. Autopulls from TX FIFO at byte
//     boundaries. Re-arms on CS rising via `jmp pin restart`.
//   - sm_rx (spi_slave_rx program): samples MOSI on every SCK rising
//     edge while CSn is LOW. Autopushes RX FIFO at byte boundaries.
//     Re-arms on CS rising.
//
// PIO replaces PL022 because the polled-loop and per-byte FIFO
// management of PL022 had enough latency to occasionally underrun
// the TX FIFO at 781 kHz SCK (each underrun → master gets 0xFF in
// place of a real data byte → that chunk's PEC fails on master →
// IC never marked online). PIO bit timing is deterministic and
// peripheral-driven; the CPU just keeps the byte-level FIFOs fed.

#include <string.h>

#include "ltc6811_emu.h"
#include "cell_state.h"
#include "pec15.h"
#include "spi_slave_pio.pio.h"

#include "hardware/pio.h"
#include "hardware/gpio.h"
#include "pico/stdlib.h"

#define PIO_INST            pio0
#define PIN_SPI_RX          16   // MOSI from master (in pin 0 of PIO IN scope)
#define PIN_SPI_CSN         17   // CSn from master  (in pin 1, also jmp_pin)
#define PIN_SPI_SCK         18   // SCK from master  (in pin 2)
#define PIN_SPI_TX          19   // MISO to master   (out pin 0 of TX SM)

static uint sm_tx;
static uint sm_rx;
static uint pio_offset_tx;
static uint pio_offset_rx;

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

// ----- PIO byte-level FIFO helpers ------------------------------------
static inline bool pio_tx_writable(void) {
    return !pio_sm_is_tx_fifo_full(PIO_INST, sm_tx);
}
static inline bool pio_rx_readable(void) {
    return !pio_sm_is_rx_fifo_empty(PIO_INST, sm_rx);
}
static inline void pio_tx_put_raw(uint8_t b) {
    // 32-bit FIFO; PIO program autopulls 8 bits from the OSR's MSB
    // end (shift_left, threshold 8). The byte we want shifted out
    // first goes in the MSB position of the 32-bit word.
    pio_sm_put(PIO_INST, sm_tx, ((uint32_t)b) << 24);
}
static inline uint8_t pio_rx_get_raw(void) {
    // RX FIFO entries are 32-bit but the byte ends up in the LOW 8
    // bits (shift_left, threshold 8 → ISR bits 0..7 hold the byte).
    return (uint8_t)(pio_sm_get(PIO_INST, sm_rx) & 0xFFu);
}

// ltc6811_emu_response_for() is defined further down, after the
// response_pool[] static is in scope.

void ltc6811_emu_init(void) {
    // Claim two state machines on PIO0.
    sm_tx = (uint)pio_claim_unused_sm(PIO_INST, true);
    sm_rx = (uint)pio_claim_unused_sm(PIO_INST, true);

    // Load both PIO programs into instruction memory.
    pio_offset_tx = pio_add_program(PIO_INST, &spi_slave_tx_program);
    pio_offset_rx = pio_add_program(PIO_INST, &spi_slave_rx_program);

    // Hand the four SPI pins over to PIO. Pin numbers 16/17/18/19 are
    // contiguous so a single in_base = 16 lets both SMs reference
    // MOSI / CSn / SCK by relative offset 0 / 1 / 2.
    pio_gpio_init(PIO_INST, PIN_SPI_RX);
    pio_gpio_init(PIO_INST, PIN_SPI_CSN);
    pio_gpio_init(PIO_INST, PIN_SPI_SCK);
    pio_gpio_init(PIO_INST, PIN_SPI_TX);

    // MISO direction = output (only the TX SM drives it; RX SM
    // doesn't touch out pins). Other three pins are inputs (default
    // after pio_gpio_init).
    pio_sm_set_consecutive_pindirs(PIO_INST, sm_tx, PIN_SPI_TX, 1, true);

    // Pulls: MOSI + SCK weakly DOWN (master MOSI is HiZ between
    // xacts; a long patch cable would otherwise float HIGH and the
    // PIO's `wait 1 pin SCK` would falsely fire on bus noise). CSn
    // pulled UP because master only pulses it LOW.
    gpio_disable_pulls(PIN_SPI_SCK);
    gpio_disable_pulls(PIN_SPI_RX);
    gpio_disable_pulls(PIN_SPI_CSN);
    gpio_pull_down(PIN_SPI_SCK);
    gpio_pull_down(PIN_SPI_RX);
    gpio_pull_up(PIN_SPI_CSN);

    // ---- TX state-machine config ----------------------------------
    pio_sm_config c_tx = spi_slave_tx_program_get_default_config(pio_offset_tx);
    // IN scope: contiguous block starting at MOSI (16). `wait 0 pin 1`
    // = CSn LOW; `wait 0 pin 2` / `wait 1 pin 2` = SCK falling/rising.
    sm_config_set_in_pins(&c_tx, PIN_SPI_RX);
    // OUT scope: MISO at 19, 1 pin wide. `out pins, 1` writes a single
    // bit to MISO.
    sm_config_set_out_pins(&c_tx, PIN_SPI_TX, 1);
    // JMP pin: CSn. `jmp pin restart` re-arms when CSn goes HIGH.
    sm_config_set_jmp_pin(&c_tx, PIN_SPI_CSN);
    // Autopull at 8 bits; shift_right=false → MSB shifts out first
    // (matches LTC6811 wire-order MSB-first).
    sm_config_set_out_shift(&c_tx, false /* shift_right */, true /* autopull */, 8);
    // Give the TX half of the FIFO 8 entries (4 default + 4 from
    // joining the unused RX half). Buys headroom against bursty CPU.
    sm_config_set_fifo_join(&c_tx, PIO_FIFO_JOIN_TX);
    pio_sm_init(PIO_INST, sm_tx, pio_offset_tx, &c_tx);

    // ---- RX state-machine config ----------------------------------
    pio_sm_config c_rx = spi_slave_rx_program_get_default_config(pio_offset_rx);
    sm_config_set_in_pins(&c_rx, PIN_SPI_RX);
    sm_config_set_jmp_pin(&c_rx, PIN_SPI_CSN);
    // Autopush at 8 bits, shift_left so first sampled bit ends up at
    // ISR bit 7 (MSB-first reception).
    sm_config_set_in_shift(&c_rx, false /* shift_right */, true /* autopush */, 8);
    sm_config_set_fifo_join(&c_rx, PIO_FIFO_JOIN_RX);
    pio_sm_init(PIO_INST, sm_rx, pio_offset_rx, &c_rx);

    // Pre-load the TX FIFO with a few 0xFF pad bytes so the very
    // first CS-fall after init finds MISO ready to drive HIGH (idle
    // MISO state). Subsequent CS-rising handlers re-prime in the
    // service loop.
    for (int i = 0; i < 4; i++) pio_tx_put_raw(0xFFu);

    // Bring both SMs out of reset and let them run.
    pio_sm_set_enabled(PIO_INST, sm_tx, true);
    pio_sm_set_enabled(PIO_INST, sm_rx, true);

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

// Each "response" is 4 bytes of pad (master ignores during its cmd
// phase) + 80 bytes of LTC chain data (10 chips * 8 bytes). The pad
// realigns our streamed buffer to where the master expects the slave
// to start replying.
#define RESPONSE_PAD 4
#define RESPONSE_LEN (RESPONSE_PAD + 80)

// 6 pre-built response buffers indexed by cmd: RDCVA/B/C/D + RDAUXA/B.
// Computed at init (and on every cell/temp change via the USB cmds).
// On cmd parse we just swap which buffer the streamer reads from.
enum { RSP_RDCVA, RSP_RDCVB, RSP_RDCVC, RSP_RDCVD, RSP_RDAUXA, RSP_RDAUXB, RSP_COUNT };
static uint8_t response_pool[RSP_COUNT][RESPONSE_LEN];
static const uint8_t *current_response = response_pool[RSP_RDCVA];  // default

// TX-snapshot diagnostic. Records every byte we push to TX FIFO during
// a single transaction so the host can verify the streamer's output
// matches the intended response_pool.
static uint8_t  tx_snap[RESPONSE_LEN];
static uint16_t tx_snap_idx          = 0;
static uint16_t tx_snap_cmd_recorded = 0xFFFFu;
static uint8_t  tx_snap_published[RESPONSE_LEN];
static uint16_t tx_snap_published_len = 0;
static uint16_t tx_snap_published_cmd = 0xFFFFu;

// RX-snapshot diagnostic, mirror of tx_snap: records the first
// LTC_RESPONSE_LEN bytes the master clocked into us on MOSI.
static uint8_t  rx_snap[RESPONSE_LEN];
static uint16_t rx_snap_idx          = 0;
static uint8_t  rx_snap_published[RESPONSE_LEN];
static uint16_t rx_snap_published_len = 0;

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

const uint8_t *ltc6811_emu_last_rx_snapshot(uint16_t *out_len) {
    if (out_len) *out_len = rx_snap_published_len;
    return rx_snap_published;
}

// Push one byte to PIO TX FIFO and record it in the per-xact
// snapshot. Returns 1 if the byte went in, 0 if FIFO is full.
static inline int tx_push(uint8_t b) {
    if (!pio_tx_writable()) return 0;
    pio_tx_put_raw(b);
    if (tx_snap_idx < RESPONSE_LEN) {
        tx_snap[tx_snap_idx++] = b;
    }
    return 1;
}

// Service loop:
//   - On CS rising (xact ended): drain any RX tail bytes into the
//     current snapshot, freeze TX+RX snapshots, clear PIO FIFOs,
//     restart PIO SMs to a clean state, pre-load 4 pad bytes for the
//     next CS-fall, reset framing state.
//   - On CS low (active xact): pump data bytes from current_response
//     into TX FIFO after cmd is parsed. The PIO TX SM autopulls
//     bytes at its own pace; CPU just has to keep the FIFO non-empty.
//   - Always: drain RX FIFO, parse cmd at first 4-byte chunk.
//
// With PIO, the bit-level timing is hardware-driven (deterministic
// 1-2 PIO cycles per SCK edge) so CPU service-loop latency only
// affects how full the FIFOs stay -- not bit alignment. As long as
// we keep TX FIFO non-empty during the data phase, MISO is correct.
void ltc6811_emu_service(void) {
    static uint8_t  rx_buf[4];
    static uint8_t  rx_idx              = 0;
    static int      tx_data_idx         = 0;   // index into current_response[RESPONSE_PAD..]
    static int      cmd_parsed_this_xact = 0;
    static int      cs_was_low           = 0;
    static int      response_init_done   = 0;

    if (!response_init_done) {
        rebuild_all_responses();
        current_response = response_pool[RSP_RDCVA];
        response_init_done = 1;
    }

    int cs_now = gpio_get(PIN_SPI_CSN);

    if (cs_was_low && cs_now) {
        // CS rising edge: drain any RX bytes still in PIO FIFO from
        // the just-finished xact INTO the outgoing snapshot, then
        // freeze, then clear FIFOs + restart SMs to a clean state.
        while (pio_rx_readable()) {
            uint8_t tail = pio_rx_get_raw();
            g_ltc_stats.last_rx[g_ltc_stats.rx_byte_count % 8u] = tail;
            g_ltc_stats.rx_byte_count++;
            if (rx_snap_idx < RESPONSE_LEN) {
                rx_snap[rx_snap_idx++] = tail;
            }
        }

        tx_snap_published_len = tx_snap_idx;
        tx_snap_published_cmd = tx_snap_cmd_recorded;
        for (uint16_t i = 0; i < tx_snap_idx; i++) tx_snap_published[i] = tx_snap[i];
        tx_snap_idx          = 0;
        tx_snap_cmd_recorded = 0xFFFFu;

        rx_snap_published_len = rx_snap_idx;
        for (uint16_t i = 0; i < rx_snap_idx; i++) rx_snap_published[i] = rx_snap[i];
        rx_snap_idx          = 0;

        // Clear PIO FIFOs and force SMs back to the program's start
        // address. Without this, a stale byte left in TX FIFO from
        // the previous xact would be the first byte master clocks on
        // the next CS-fall.
        pio_sm_clear_fifos(PIO_INST, sm_tx);
        pio_sm_clear_fifos(PIO_INST, sm_rx);
        pio_sm_restart(PIO_INST, sm_tx);
        pio_sm_restart(PIO_INST, sm_rx);
        // Force PC back to each program's entry point.
        pio_sm_exec(PIO_INST, sm_tx,
                    pio_encode_jmp(pio_offset_tx + spi_slave_tx_offset_restart));
        pio_sm_exec(PIO_INST, sm_rx,
                    pio_encode_jmp(pio_offset_rx + spi_slave_rx_offset_restart));

        // Pre-load 4 pad bytes for the NEXT xact's cmd phase. With
        // these in TX FIFO before CS falls again, the TX SM autopulls
        // and drives MISO immediately on the first SCK edge -- no
        // dependency on CPU latency for the cmd-phase MISO state.
        for (int i = 0; i < RESPONSE_PAD; i++) pio_tx_put_raw(0xFFu);

        rx_idx               = 0;
        tx_data_idx          = 0;
        cmd_parsed_this_xact = 0;
        g_ltc_stats.n_cs_cycles++;
    }
    cs_was_low = !cs_now;

    if (!cs_now && cmd_parsed_this_xact) {
        // Cmd parsed mid-xact: pump data bytes from current_response
        // into TX FIFO until either FIFO is full or buffer exhausted.
        // PIO SM autopulls at its own pace; we just keep the FIFO fed.
        while (pio_tx_writable()) {
            uint8_t b = (tx_data_idx < (RESPONSE_LEN - RESPONSE_PAD))
                            ? current_response[RESPONSE_PAD + tx_data_idx]
                            : 0xFFu;
            if (!tx_push(b)) break;
            if (tx_data_idx < (RESPONSE_LEN - RESPONSE_PAD)) tx_data_idx++;
        }
    }

    // RX side: drain bytes, parse cmd at first 4-byte chunk, update
    // response buffer pointer.
    while (pio_rx_readable()) {
        uint8_t b = pio_rx_get_raw();
        g_ltc_stats.last_rx[g_ltc_stats.rx_byte_count % 8u] = b;
        g_ltc_stats.rx_byte_count++;
        if (rx_snap_idx < RESPONSE_LEN) {
            rx_snap[rx_snap_idx++] = b;
        }
        rx_buf[rx_idx++] = b;
        if (rx_idx < 4) continue;
        rx_idx = 0;

        // 11-bit command in rx_buf[0..1].
        uint16_t cmd = (uint16_t)(((rx_buf[0] & 0x07u) << 8) | rx_buf[1]);
        g_ltc_stats.n_spi_xact++;
        g_ltc_stats.last_cmd = cmd;

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
                    if ((cmd & 0x7C0u) == 0x260u || (cmd & 0x7C0u) == 0x340u) is_known = 1;
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
