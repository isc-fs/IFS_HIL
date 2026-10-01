// In-memory state for the simulated chain: cell voltages (mV per
// cell, per LTC chip) and auxiliary readings (used by AMS firmware
// for cell temperatures via NTC dividers).
//
// Chain layout matches what the AMS firmware expects (see
// `kLtcChainLength` in `firmware/Core/Inc/app/ams_config.hpp`):
//   - 10 LTC6811 chips in series
//   - 12 cell slots per chip (cells 1..12)
//   - 5 GPIO pins per chip mapped to NTC sensors (cells per module
//     vs aux channels is a firmware-side wiring decision; we just
//     present 12 cells + 5 aux per chip and let the firmware index
//     into them as configured)

#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LTC_CHAIN_LEN     10
#define CELLS_PER_LTC     12
// Sized to cover the full ADG731 32-channel selector space the AMS
// uses to route up to 20 NTCs per LTC into the LTC's GPIO1 (see
// IFS_HIL#41 + ams_config.hpp::Adg731ChannelMap). `temp_dC[ltc][N]`
// is the value AMS sees on AUX1 when the mux selector points at
// channel N -- the Pico snoops WRCOMM to track which N is active and
// feeds the right slot into the RDAUXA AUX1 bytes. Slots 0..4 also
// double as the legacy AUX2..5 fallback in the RDAUXA/B bytes that
// AMS doesn't read but the dC_to_ltc encoder still populates for
// PEC15 stability.
#define AUX_PER_LTC       32

// Default seeds, matching `BmsService::seed_for_hil_stub`.
#define DEFAULT_CELL_MV   3750u
#define DEFAULT_TEMP_DC    250    // deci-degC

typedef struct {
    uint16_t cell_mV[LTC_CHAIN_LEN][CELLS_PER_LTC];
    int16_t  temp_dC[LTC_CHAIN_LEN][AUX_PER_LTC];
} cell_state_t;

extern cell_state_t g_state;

void cell_state_reset(void);
void cell_state_set_cell(uint8_t ltc_idx, uint8_t cell_idx, uint16_t mV);
void cell_state_set_temp(uint8_t ltc_idx, uint8_t sensor_idx, int16_t dC);
void cell_state_set_all_cells(uint16_t mV);
void cell_state_set_all_temps(int16_t dC);

#ifdef __cplusplus
}
#endif
