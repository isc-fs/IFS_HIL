#include "cell_state.h"

cell_state_t g_state;

void cell_state_reset(void) {
    for (uint8_t i = 0; i < LTC_CHAIN_LEN; i++) {
        for (uint8_t c = 0; c < CELLS_PER_LTC; c++) {
            g_state.cell_mV[i][c] = DEFAULT_CELL_MV;
        }
        for (uint8_t a = 0; a < AUX_PER_LTC; a++) {
            g_state.temp_dC[i][a] = DEFAULT_TEMP_DC;
        }
    }
}

void cell_state_set_cell(uint8_t ltc_idx, uint8_t cell_idx, uint16_t mV) {
    if (ltc_idx >= LTC_CHAIN_LEN || cell_idx >= CELLS_PER_LTC) return;
    g_state.cell_mV[ltc_idx][cell_idx] = mV;
}

void cell_state_set_temp(uint8_t ltc_idx, uint8_t sensor_idx, int16_t dC) {
    if (ltc_idx >= LTC_CHAIN_LEN || sensor_idx >= AUX_PER_LTC) return;
    g_state.temp_dC[ltc_idx][sensor_idx] = dC;
}

void cell_state_set_all_cells(uint16_t mV) {
    for (uint8_t i = 0; i < LTC_CHAIN_LEN; i++) {
        for (uint8_t c = 0; c < CELLS_PER_LTC; c++) {
            g_state.cell_mV[i][c] = mV;
        }
    }
}

void cell_state_set_all_temps(int16_t dC) {
    for (uint8_t i = 0; i < LTC_CHAIN_LEN; i++) {
        for (uint8_t a = 0; a < AUX_PER_LTC; a++) {
            g_state.temp_dC[i][a] = dC;
        }
    }
}
