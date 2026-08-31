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
#include <math.h>

#include "ltc6811_emu.h"
#include "cell_state.h"
#include "pec15.h"
#include "spi_slave_pio.pio.h"

#include "hardware/pio.h"
#include "hardware/dma.h"
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
static int  dma_tx_chan;

// Forward decls -- definitions live further down (after the cell-state
// helpers and response_pool[] are in scope), but ltc6811_emu_init()
// needs to call them to pre-load the FULL FIFO at boot so the very
// first xact (AMS boot-discovery RDCFGA) doesn't underrun.
#define RESPONSE_PAD 4
#define RESPONSE_LEN (RESPONSE_PAD + 80)
enum { RSP_RDCVA, RSP_RDCVB, RSP_RDCVC, RSP_RDCVD, RSP_RDAUXA, RSP_RDAUXB, RSP_COUNT };
static uint8_t response_pool[RSP_COUNT][RESPONSE_LEN];
static const uint8_t *current_response = response_pool[RSP_RDCVA];
// Response buffer snapshotted at CS-rising (= pre-load time). chip 0's
// 8 data bytes are BOTH pre-loaded and pumped from THIS pointer, so the
// chip stays self-consistent (data + PEC from a single response) even
// when the command — parsed mid-xact — swaps `current_response`. chip
// 1..9 are pumped from the freshly parsed `current_response` and are
// gated until the parse completes. Without this split, the pump leaked
// the previous xact's response into chip 1's data bytes while chip 1's
// PEC came from the new response → chip-1-only PEC failures on the
// master (IFS_HIL#44 residual). See ltc6811_emu_service().
static const uint8_t *preparse_response = response_pool[RSP_RDCVA];
static void rebuild_all_responses(void);

// Per-module response-suppression mask. See header docstring for
// semantics. Read on every TX byte push in the hot path, so keep
// it as a plain uint8 -- writes are atomic on Cortex-M0+, no
// locking needed.
static volatile uint8_t g_stop_module_mask = 0u;

// ADG731 mux selector state -- updated by snooping AMS WRCOMM
// commands (cmd 0x721). The AMS poll loop cycles through every NTC
// position by writing the ADG731 selector via WRCOMM + STCOMM and
// then reading AUX1 via RDAUXA. The Pico mirrors that scan by
// looking up `g_state.temp_dC[ltc][g_adg731_ch]` when it builds the
// RDAUXA AUX1 bytes -- so injecting at temp_dC[ltc][N] causes AMS
// to see that temperature only when its mux is at channel N.
//
// AMS broadcasts the SAME selector to every IC in the chain (see
// bms_poll_task.cpp's per_ic_payload loop), so a single global
// selector suffices today. If AMS ever drives ICs independently, lift
// this to a per-ltc array of 10 uint8_t -- everything else in the
// snoop path stays.
static volatile uint8_t g_adg731_ch = 0u;

uint8_t ltc6811_emu_get_adg731_ch(void)         { return g_adg731_ch; }

void ltc6811_emu_set_stop_mask(uint8_t mask) { g_stop_module_mask = mask & 0x1Fu; }
uint8_t ltc6811_emu_get_stop_mask(void)      { return g_stop_module_mask; }

// Decide whether the byte at `tx_data_idx` (offset into the data
// section, i.e. excluding RESPONSE_PAD) belongs to a suppressed
// chain position. Bytes are laid out 8-per-chain-position; each pair
// (positions 2N, 2N+1) is one AMS module, so mask bit N suppresses
// data bytes [N*16 .. N*16+15].
static inline uint8_t maybe_suppress_data(uint8_t b, int tx_data_idx) {
    uint8_t mask = g_stop_module_mask;
    if (mask == 0u) return b;
    int chain_pos = tx_data_idx >> 3;          // /8
    int module    = chain_pos >> 1;            // /2 (two LTCs per module)
    if (mask & (1u << module)) return 0xFFu;
    return b;
}

// LTC6811 cell voltage encoding: each cell is uint16_t in units of
// 100 microvolts (0.1 mV). cell_mV * 10 gives the on-wire value.
static inline uint16_t mV_to_ltc(uint16_t mV) {
    // Saturate to uint16 max (≈ 6.55 V) -- way above any sane cell.
    uint32_t v = (uint32_t)mV * 10u;
    return (v > 0xFFFFu) ? 0xFFFFu : (uint16_t)v;
}

// Temperature-to-NTC-divider voltage. AMS firmware decodes AUX as a
// resistor divider with a Beta-model NTC:
//   V_aux_mV  = NtcVrefMv * R_ntc / (NtcSeriesR + R_ntc)
//   R_ntc(T)  = NtcR25 * exp(Beta * (1/T_K - 1/T0))
// per Core/Inc/app/ams_config.hpp:
//   NtcVrefMv    = 3000     (LTC6811 VREF2)
//   NtcSeriesR   = 10000 Ω  (divider pull-up)
//   NtcR25       = 10000 Ω
//   NtcBeta      = 3380 K
//   NtcT0Kelvin  = 298.15 K (25 °C)
//
// For target temp dC (deci-°C, 250 = 25 °C):
//   T_K   = dC/10 + 273.15
//   R     = 10000 * exp(3380 * (1/T_K - 1/298.15))
//   V_aux = 3000 * R / (10000 + R)
//   aux_u16 = V_aux * 10   (u16 100 µV units the LTC chip emits)
//
// Earlier stand-in was off by 10× and the firmware's Steinhart
// decoder mapped my "25 °C" output (150 mV) to ~113 °C, tripping the
// CellOverTempC = 60 °C predicate and forcing the FSM to Error.
static inline uint16_t dC_to_ltc(int16_t dC) {
    const float t_K  = (float)dC / 10.0f + 273.15f;
    const float beta = 3380.0f;
    const float t0   = 298.15f;
    const float r25  = 10000.0f;
    const float rs   = 10000.0f;
    const float vref = 3000.0f;            // mV
    const float r    = r25 * expf(beta * (1.0f / t_K - 1.0f / t0));
    const float v_aux_mV = vref * r / (rs + r);
    const float aux_100uV = v_aux_mV * 10.0f;
    if (aux_100uV < 0.0f)        return 0u;
    if (aux_100uV > 65535.0f)    return 65535u;
    return (uint16_t)(aux_100uV + 0.5f);
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

    // MISO drive strength: bump to 12 mA (default 4 mA) so we can
    // ramp a long patch-cable's capacitance fast enough at 781 kHz
    // SCK. 4 mA + ~100 pF wire cap = ~80 ns rise time which is fine
    // in theory, but the master's MISO sample window in Mode 3 is
    // only ~640 ns and any slack helps.
    gpio_set_drive_strength(PIN_SPI_TX, GPIO_DRIVE_STRENGTH_12MA);
    gpio_set_slew_rate(PIN_SPI_TX, GPIO_SLEW_RATE_FAST);

    // Pulls. CSn UP because master only briefly pulses it LOW.
    // SCK UP because in Mode 3 SCK idles HIGH -- a pull-DOWN would
    // briefly pull SCK low if the master ever releases the line
    // (e.g. during the SS-idleness gap between CS-fall and the first
    // SCK pulse), creating a spurious falling edge our PIO would
    // mistake for the first real edge. MOSI weakly DOWN so a long
    // patch cable left HiZ between xacts can't float HIGH and bias
    // the slave's first sample.
    gpio_disable_pulls(PIN_SPI_SCK);
    gpio_disable_pulls(PIN_SPI_RX);
    gpio_disable_pulls(PIN_SPI_CSN);
    gpio_pull_up(PIN_SPI_SCK);
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

    // Pre-load the FULL FIFO depth (8 entries) so the very first
    // xact -- which is typically the AMS boot-discovery RDCFGA --
    // has a complete cmd-phase + first-4-data-bytes ready without
    // depending on CPU latency. If we only pre-loaded the 4 pad
    // bytes and CPU push of the first data byte was late, the boot
    // discovery's PEC would fail, ERROR would latch in BKP RAM, and
    // the FSM would be stuck in Error for the rest of the session
    // (until power-cycle) regardless of how well subsequent xacts
    // worked. Build response_pool now so chip 0's first 4 data
    // bytes are available to pre-load here.
    rebuild_all_responses();
    current_response   = response_pool[RSP_RDCVA];
    preparse_response  = current_response;
    for (int i = 0; i < 4; i++) pio_tx_put_raw(0xFFu);
    for (int i = 0; i < 4; i++) pio_tx_put_raw(preparse_response[RESPONSE_PAD + i]);

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
        // RDAUXA returns AUX1..3 (= LTC GPIO1..GPIO3 in 100 uV units).
        // The AMS only ever reads AUX1: GPIO1 is the output of an
        // ADG731 32:1 analog mux that steers one of up to 20 NTCs in
        // at a time, selected via WRCOMM+STCOMM. We snoop the
        // selector (g_adg731_ch) and respond with the value the AMS
        // wired-in NTC at that mux channel would have reported.
        //
        // AUX2 + AUX3 don't correspond to anything AMS reads, but we
        // still populate them from the legacy slots 1 + 2 so the
        // pre-mux SET_TEMP API keeps working (test code that bumps
        // sensor=0 still routes through slot 0 = mux channel 0,
        // which is the channel AMS visits at every full scan).
        uint16_t v_aux1 = dC_to_ltc(g_state.temp_dC[ltc_idx][g_adg731_ch]);
        out[0] = (uint8_t)(v_aux1 & 0xFFu);
        out[1] = (uint8_t)((v_aux1 >> 8) & 0xFFu);
        for (uint8_t i = 1; i < 3; i++) {
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

// (RESPONSE_PAD, RESPONSE_LEN, RSP_* enum, response_pool[],
// current_response are forward-declared above so ltc6811_emu_init can
// reference them. response_pool[] storage and current_response are
// also defined there -- not duplicated here.)

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
    static int      is_wrcomm_xact       = 0;   // true if this xact's cmd was WRCOMM (0x721)

    if (!response_init_done) {
        rebuild_all_responses();
        current_response   = response_pool[RSP_RDCVA];
        preparse_response  = current_response;
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

        // Pre-load the FULL FIFO depth (8 entries = 8 bytes) at
        // CS-rising so the cmd-phase + first 4 data bytes are
        // guaranteed in flight before master clocks them, no
        // dependency on CPU latency for the cmd→data boundary.
        //   bytes 0..3  = 0xFF pad (master ignores during cmd phase)
        //   bytes 4..7  = first 4 bytes of current_response data
        //                 section (= chip 0's first 4 data bytes)
        // For uniform-cell test setups all RDCV* groups have
        // identical chip-0 bytes, so the pre-load is correct
        // regardless of which RDCV cmd actually comes. For non-
        // uniform cells, chip 0's first 4 bytes may be wrong if cmd
        // != current_response -- handled later by cmd-parse swap.
        //
        // Snapshot the response we're committing chip 0 to NOW. The
        // command for THIS xact isn't known yet (master hasn't clocked
        // it in), so chip 0's 8 bytes are necessarily built from the
        // previous xact's response. We pump the rest of chip 0 from the
        // same snapshot (see the data pump below) so chip 0 stays
        // self-consistent — data + PEC from one response, PEC-valid on
        // the master even when it's a poll stale. chip 1..9 wait for the
        // real command and use `current_response`.
        preparse_response = current_response;
        for (int i = 0; i < RESPONSE_PAD; i++) pio_tx_put_raw(0xFFu);
        // Pre-load of chip 0's first 4 data bytes -- these belong to
        // chain position 0 (= module 0), so route through the same
        // suppress filter as the main pump or a STOP_REPLY on module 0
        // would still leak real data through the pre-load window.
        for (int i = 0; i < 4; i++) {
            pio_tx_put_raw(maybe_suppress_data(
                preparse_response[RESPONSE_PAD + i], i));
        }

        rx_idx               = 0;
        tx_data_idx          = 4;   // start CPU push at byte 4 (we pre-loaded 0..3)
        cmd_parsed_this_xact = 0;
        is_wrcomm_xact       = 0;
        g_ltc_stats.n_cs_cycles++;
    }
    cs_was_low = !cs_now;

    if (!cs_now) {
        // Pump data bytes into the TX FIFO, choosing the source per
        // chain position so a mid-xact command swap can't split a
        // single chip across two responses (the IFS_HIL#44 chip-1
        // residual):
        //   - chip 0 (data idx 0..7): always `preparse_response` (the
        //     pre-load-time snapshot). chip 0's first 4 bytes were
        //     already pre-loaded from it; completing chip 0 from the
        //     same buffer keeps data + PEC consistent → PEC-valid even
        //     if it's one poll stale.
        //   - chip 1..9 (data idx >= 8): `current_response`, but ONLY
        //     once the command has been parsed. Before the parse the
        //     real command is unknown, so we must NOT emit chip 1+ data
        //     yet — break and let the FIFO ride on the pre-loaded chip-0
        //     bytes (≈82 µs of headroom; the 4-byte command finishes
        //     at ≈41 µs, so chip 1's first byte, clocked at ≈123 µs,
        //     is always covered by the parsed response).
        while (pio_tx_writable() &&
               tx_data_idx < (RESPONSE_LEN - RESPONSE_PAD)) {
            const uint8_t *src;
            if (tx_data_idx < 8) {
                src = preparse_response;
            } else if (cmd_parsed_this_xact) {
                src = current_response;
            } else {
                break;  // chip 1+ gated until the command is known
            }
            uint8_t b = maybe_suppress_data(
                src[RESPONSE_PAD + tx_data_idx], tx_data_idx);
            if (!tx_push(b)) break;
            tx_data_idx++;
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
        // WRCOMM payload snoop -- when a WRCOMM xact is in progress
        // and we've just buffered the 6th RX byte (= first per-IC
        // payload's p[1]), decode the ADG731 selector. p[0]/p[1]
        // encoding from ltc6811::pack_adg731_select:
        //   p[0] = (icom << 4) | data_hi    -> data_hi = p[0] & 0x0F
        //   p[1] = (data_lo << 4) | fcom    -> data_lo = p[1] >> 4
        //   data = 0x80 | ((ch & 0x1F) << 1) -> ch = (data >> 1) & 0x1F
        // AMS broadcasts the same selector to every IC, so capturing
        // the FIRST per-IC payload is sufficient (don't need to walk
        // through all 80 payload bytes per xact).
        if (is_wrcomm_xact && rx_snap_idx == 6) {
            uint8_t p0 = rx_snap[4];
            uint8_t p1 = rx_snap[5];
            // Only update on a REAL selector transmission. Real
            // writes use icom=0x8 ("drive CSBM low for this slot")
            // so p[0]'s upper nibble is 0x8. AMS also sends no-op
            // WRCOMMs (icom=0xF, p[0]=0xFF/p[1]=0xFF) to clear the
            // COMM register between scans; those would naively
            // decode to channel 31 -- skip.
            if (((p0 >> 4) & 0x0Fu) == 0x8u) {
                uint8_t data = (uint8_t)(((p0 & 0x0Fu) << 4) | ((p1 >> 4) & 0x0Fu));
                uint8_t new_ch = (uint8_t)((data >> 1) & 0x1Fu);
                if (new_ch != g_adg731_ch) {
                    // response_pool[RDAUXA] is prebuilt -- its AUX1
                    // bytes encode temp_dC[ltc][g_adg731_ch] at the
                    // time the pool was last refreshed. The selector
                    // just changed, so rebuild RDAUXA NOW (before
                    // AMS's STCOMM + ADAX + RDAUXA reaches us). The
                    // rebuild is ~100 us; AMS waits ~2 ms after
                    // STCOMM, so we have plenty of margin.
                    g_adg731_ch = new_ch;
                    build_one_response(RSP_RDAUXA, 0, 1, 0);
                }
            }
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
                    is_known = 1;
                    is_wrcomm_xact = 1;
                    break;
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
