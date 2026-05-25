// LTC6811 chain protocol handler. Skeleton -- recognises the small
// set of commands the AMS firmware actually issues during a normal
// poll cycle, generates correct-length PEC15-trailered responses,
// and stubs the rest.
//
// The MLC's STM32 is the SPI master. We see a sequence of frames
// per chip-select assertion:
//
//   1. 4 bytes from master:  [cmd_hi cmd_lo pec_hi pec_lo]
//                            cmd is 11-bit (top 5 bits = address;
//                            lower 11 bits = command code)
//   2. Either (write):  master sends N * 8 bytes (6 data + 2 PEC per chip)
//      Or     (read):   master clocks N * 8 bytes, slave drives MISO
//
// We keep state between transactions only for ADC-conversion timing
// (ADCV -> wait -> RDCV*); everything else is stateless.

#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t n_spi_xact;        // every 4-byte window parsed
    uint16_t last_cmd;          // most recent ANY-value cmd parse
    uint16_t last_ltc_cmd;      // most recent KNOWN-LTC cmd (filter: only the small whitelist)
    uint32_t n_valid_cmds;      // counter of whitelisted LTC cmds recognised
    uint8_t  last_rx[8];        // sliding window of last 8 raw RX bytes
    uint32_t rx_byte_count;     // monotonic byte counter (NOT reset by RESET_STATE)
} ltc_stats_t;

extern ltc_stats_t g_ltc_stats;

// Initialise SPI0 in slave mode on GP16 (RX), GP18 (SCK), GP19 (TX),
// GP17 (CSn). Idempotent.
void ltc6811_emu_init(void);

// Called from the SPI IRQ context when bytes are available. Pulls
// from the RX FIFO, decodes the command, queues the response into
// the TX FIFO.
void ltc6811_emu_service(void);

// Reset stats counters back to zero. Used by the host-side
// `RESET_STATE` command for clean tests.
void ltc6811_emu_reset_stats(void);

#ifdef __cplusplus
}
#endif
