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
#include "ntc_aux_table.h"
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
// chip 0 used to be pumped from a separate CS-rising snapshot
// (`preparse_response`) so its data and PEC stayed self-consistent
// while a mid-xact parse swapped `current_response` under it — the
// IFS_HIL#44 chip-1 fix. That snapshot predates the command, so it
// also made chain position 0 report the previous command's data
// (IFS_HIL#116). The byte-2 parse decodes the opcode before ANY data
// byte is emitted, so every chain position now reads from one
// already-correct `current_response` and the snapshot is gone.
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

// Temperature-to-NTC-divider voltage.
//
// The AMS recovers thermistor resistance from the observed AUX voltage as
//   R_ntc = NtcPullupOhm * V_aux / (NtcVrefMv - V_aux)
// and then converts R -> T with the MANUFACTURER R-T TABLE
// (Core/Inc/app/ntc_table.hpp, generated from docs/ntc_rt_table.csv), not a
// single-beta fit. Per ams_config.hpp:
//   NtcVrefMv    = 3000     (LTC6811 VREF2)
//   NtcPullupOhm = 6800     (R145 / R170 pull-up to VREF2)
//
// This emulator used to invert a BETA MODEL with two constants that the AMS
// firmware no longer has -- a 10 kOhm series resistor and beta = 3380 (the
// beta of a Murata NCP15XH103J, not the fitted Fenghua CMFB103F3950 whose
// beta is 3950). Both were removed from the AMS in #457 when the real table
// was ported; the emulator was never updated and kept citing them.
//
// The result was a silent calibration error in every temperature the bench
// asserted: seeding 25 C read 34 C, 10 C read 20 C, 60 C read 65 C
// (IFS_HIL#117). The 25 C case shows it is the pull-up rather than beta --
// at T0 the beta term cancels, so a matched divider would read exactly 25.
// Overstating the pull-up as 10 k emits 1500 mV where the AMS expects
// 1785.7 mV, and the AMS reads that back as 34 C.
//
// So: emit the AUX voltage straight from the SAME table the AMS decodes
// with, interpolated to a tenth of a degree. The round-trip is then exact by
// construction, rather than two independent models happening to agree.
static inline uint16_t dC_to_ltc(int16_t dC) {
    const int32_t t_min_dC = (int32_t)NTC_AUX_T_MIN_C * 10;
    const int32_t t_max_dC = (int32_t)NTC_AUX_T_MAX_C * 10;
    const int32_t d = (int32_t)dC;

    if (d <= t_min_dC) return k_ntc_aux_100uV[0];
    if (d >= t_max_dC) return k_ntc_aux_100uV[NTC_AUX_COUNT - 1];

    const int32_t idx  = (d - t_min_dC) / 10;
    const int32_t frac = (d - t_min_dC) - idx * 10;      // tenths into the step
    const int32_t a    = (int32_t)k_ntc_aux_100uV[idx];
    const int32_t b    = (int32_t)k_ntc_aux_100uV[idx + 1];

    // b < a: the table falls with temperature. Signed arithmetic throughout.
    return (uint16_t)(a + ((b - a) * frac) / 10);
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
// FDEBUG_TXSTALL bit for the TX SM. Sticky: set when the SM stalls on
// an empty TX FIFO, cleared by writing a 1 back. Used to tell whether
// the 4-byte command-phase pad ever runs dry before the CPU's first
// data push -- i.e. whether the ~20 us byte-2 parse budget is met.
static inline uint32_t txstall_mask(void) {
    return 1u << (PIO_FDEBUG_TXSTALL_LSB + sm_tx);
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

    // Pre-load the 4-byte command-phase pad only.
    //
    // The very first xact is typically the AMS boot-discovery RDCFGA,
    // and a PEC failure there latches ERROR in BKP RAM for the rest of
    // the session (power-cycle to clear) no matter how well later
    // xacts go -- so this pre-load must not depend on CPU latency.
    // Four pad bytes is exactly the length of the command phase, and
    // the byte-2 parse in ltc6811_emu_service() then has ~20 us to
    // push chain position 0's first data byte before the master clocks
    // it. Earlier firmware also pre-loaded 4 data bytes here, which
    // removed that dependency entirely but forced chip 0 to carry the
    // PREVIOUS command's data (IFS_HIL#116).
    rebuild_all_responses();
    current_response   = response_pool[RSP_RDCVA];
    for (int i = 0; i < RESPONSE_PAD; i++) pio_tx_put_raw(0xFFu);

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

// Per-command statistics. The single-buffered tx/rx snapshots cannot say WHICH
// commands the master issues: they hold only the last transaction, and a host
// poll costs ~50 ms over USB CDC while the AMS reads in a burst -- so the
// survivor is always whichever command came last, and RDCVA/B/C look as though
// they are never sent (IFS_HIL#116). Counting instead of sampling removes the
// aliasing entirely.
//
// `xact_rx_count` is the UNCAPPED number of bytes clocked in this transaction.
// rx_snap_idx saturates at RESPONSE_LEN, and SPI is full duplex, so this is the
// true count of bytes the master clocked -- the figure needed to tell a
// 10-device chain read (4 + 80) from a 9-device one (4 + 72).
#define CMD_STATS_MAX 24
typedef struct {
    uint16_t cmd;
    uint32_t count;
    uint16_t last_len;
    uint16_t min_len;
    uint16_t max_len;
} cmd_stat_t;
static cmd_stat_t g_cmd_stats[CMD_STATS_MAX];
static uint8_t    g_cmd_stats_n   = 0;
static uint32_t   g_cmd_stats_lost = 0;   // distinct commands past CMD_STATS_MAX
static uint16_t   xact_rx_count   = 0;

static void cmd_stats_record(uint16_t cmd, uint16_t len) {
    for (uint8_t i = 0; i < g_cmd_stats_n; i++) {
        if (g_cmd_stats[i].cmd == cmd) {
            g_cmd_stats[i].count++;
            g_cmd_stats[i].last_len = len;
            if (len < g_cmd_stats[i].min_len) g_cmd_stats[i].min_len = len;
            if (len > g_cmd_stats[i].max_len) g_cmd_stats[i].max_len = len;
            return;
        }
    }
    if (g_cmd_stats_n < CMD_STATS_MAX) {
        g_cmd_stats[g_cmd_stats_n] = (cmd_stat_t){ cmd, 1u, len, len, len };
        g_cmd_stats_n++;
    } else {
        g_cmd_stats_lost++;
    }
}

uint8_t ltc6811_emu_cmd_stats_count(void) { return g_cmd_stats_n; }
uint32_t ltc6811_emu_cmd_stats_lost(void) { return g_cmd_stats_lost; }

void ltc6811_emu_cmd_stats_get(uint8_t i, uint16_t *cmd, uint32_t *count,
                               uint16_t *last_len, uint16_t *min_len,
                               uint16_t *max_len) {
    if (i >= g_cmd_stats_n) return;
    if (cmd)      *cmd      = g_cmd_stats[i].cmd;
    if (count)    *count    = g_cmd_stats[i].count;
    if (last_len) *last_len = g_cmd_stats[i].last_len;
    if (min_len)  *min_len  = g_cmd_stats[i].min_len;
    if (max_len)  *max_len  = g_cmd_stats[i].max_len;
}

void ltc6811_emu_cmd_stats_reset(void) {
    g_cmd_stats_n = 0;
    g_cmd_stats_lost = 0;
}
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
// Decode the 11-bit LTC opcode and point `current_response` at the
// matching prebuilt buffer. Split out of the RX drain so the byte-2
// early parse and the 4-byte fallback window share one decode.
static void parse_ltc_cmd(uint16_t cmd, int *is_wrcomm) {
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
            // NOTE: C fall-through means *is_wrcomm is set for every
            // opcode in this group, not just WRCOMM (0x721). Preserved
            // verbatim from the pre-#116 code so this refactor stays
            // behaviour-neutral -- see the follow-up issue.
            is_known    = 1;
            *is_wrcomm  = 1;
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
}


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

        // Transaction boundary: attribute the clocked-byte count to the
        // command that opened it, before anything is reset.
        if (tx_snap_cmd_recorded != 0xFFFFu) {
            cmd_stats_record(tx_snap_cmd_recorded, xact_rx_count);
        }
        xact_rx_count = 0;

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

        // Pre-load ONLY the 4-byte command-phase pad at CS-rising.
        //
        // Older firmware also pre-loaded chain position 0's first four
        // data bytes here, filling all 8 FIFO entries. That was safe
        // for the FIFO but structurally wrong: at CS-rising the
        // command for the NEXT xact has not been clocked in yet, so
        // chip 0 was necessarily built from the PREVIOUS xact's
        // response (IFS_HIL#116).
        //
        // The pad is exactly as long as the command phase, so the CPU
        // now supplies every data byte, gated on the byte-2 parse in
        // the RX drain below. Budget: opcode complete at ~20 us, chip
        // 0's first data byte clocked at ~41 us, and during CS-low the
        // main loop runs nothing but ltc6811_emu_service().
        for (int i = 0; i < RESPONSE_PAD; i++) pio_tx_put_raw(0xFFu);

        // TXSTALL is sticky. Clear it here -- CS is HIGH and the TX SM
        // was just restarted onto `wait 0 pin 1`, so it runs no `out`
        // until the next CS-fall -- and the flag read during the data
        // phase then reflects only the next xact's command phase.
        PIO_INST->fdebug = txstall_mask();

        rx_idx               = 0;
        tx_data_idx          = 0;   // CPU now pushes every data byte
        cmd_parsed_this_xact = 0;
        is_wrcomm_xact       = 0;
        g_ltc_stats.n_cs_cycles++;
    }
    cs_was_low = !cs_now;

    if (!cs_now) {
        // Pump data bytes into the TX FIFO. Nothing may be emitted
        // until the command is known -- chain position 0 included.
        //
        // Previously chip 0 (data idx 0..7) was exempted from this
        // gate and served from `preparse_response`, a snapshot taken
        // at CS-rising before the command existed. That made chain
        // position 0 report the PREVIOUS command's data: with AMS's
        // real mix (RDAUXA 2000 : each RDCV 375) module 0's cell
        // voltages tracked the NTC curve (IFS_HIL#116). The byte-2
        // parse above removes the need for the exemption, so chip 0
        // and chips 1..9 now share one gate and one buffer -- data
        // and PEC are self-consistent by construction.
        while (pio_tx_writable() &&
               tx_data_idx < (RESPONSE_LEN - RESPONSE_PAD)) {
            if (!cmd_parsed_this_xact) {
                break;  // all chain positions gated until cmd is known
            }
            if (tx_data_idx == 0 && (PIO_INST->fdebug & txstall_mask())) {
                // The 4-byte pad ran dry before we got here, so MISO
                // was undriven for part of the command phase and the
                // frame is already out of alignment. Nothing to undo
                // -- count it so the bench can see whether the ~20 us
                // parse budget is ever actually missed.
                g_ltc_stats.n_tx_stall_cmd++;
            }
            uint8_t b = maybe_suppress_data(
                current_response[RESPONSE_PAD + tx_data_idx], tx_data_idx);
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
        xact_rx_count++;
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

        // Early command decode (IFS_HIL#116).
        //
        // The 11-bit opcode is complete in bytes 0..1; bytes 2..3 are
        // only its PEC. Decoding here instead of waiting for the full
        // 4-byte window buys two byte-times -- ~20 us at the AMS's
        // ~780 kHz SCK -- before the master clocks chain position 0's
        // first data byte at ~41 us. That is the entire reason chip 0
        // can now be served from THIS xact's response instead of the
        // previous one.
        //
        // Latency is bounded by the RX FIFO, not by the CPU: these
        // command bytes sit in the 8-deep RX FIFO until drained, so
        // even a service call delayed to ~35 us reads the correct
        // opcode and still beats the data phase.
        if (rx_idx == 2 && !cmd_parsed_this_xact) {
            parse_ltc_cmd((uint16_t)(((rx_buf[0] & 0x07u) << 8) | rx_buf[1]),
                          &is_wrcomm_xact);
            cmd_parsed_this_xact = 1;
        }

        if (rx_idx < 4) continue;
        rx_idx = 0;

        // 11-bit command in rx_buf[0..1].
        uint16_t cmd = (uint16_t)(((rx_buf[0] & 0x07u) << 8) | rx_buf[1]);
        g_ltc_stats.n_spi_xact++;
        g_ltc_stats.last_cmd = cmd;

        if (!cmd_parsed_this_xact) {
            // Late path. Normally unreachable: the byte-2 parse above
            // has already decoded this xact. Kept so a malformed or
            // truncated command phase still resolves on the 4-byte
            // window rather than leaving the xact undecoded.
            parse_ltc_cmd(cmd, &is_wrcomm_xact);
            cmd_parsed_this_xact = 1;
        }
    }
}
