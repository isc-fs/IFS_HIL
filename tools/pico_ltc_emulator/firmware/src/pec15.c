// PEC15 implementation -- byte-at-a-time table-driven, computed lazily
// at first use. Matches LTC6811 datasheet pseudo-code (figure 27).

#include "pec15.h"

static uint16_t crc_table[256];
static int      table_ready = 0;

// Polynomial 0xC599 from the datasheet's CRC15 generator.
#define PEC15_POLY 0x4599u

static void init_table(void) {
    for (int i = 0; i < 256; i++) {
        uint16_t remainder = (uint16_t)(i << 7);
        for (int bit = 0; bit < 8; bit++) {
            if (remainder & 0x4000u) {
                remainder = (uint16_t)((remainder << 1) ^ PEC15_POLY);
            } else {
                remainder = (uint16_t)(remainder << 1);
            }
        }
        crc_table[i] = (uint16_t)(remainder & 0x7FFFu);
    }
    table_ready = 1;
}

uint16_t pec15_compute(const uint8_t *data, size_t len) {
    if (!table_ready) init_table();

    // Seed value is 16 per datasheet.
    uint16_t remainder = 16;
    for (size_t i = 0; i < len; i++) {
        uint8_t addr = (uint8_t)(((remainder >> 7) ^ data[i]) & 0xFFu);
        remainder = (uint16_t)((remainder << 8) ^ crc_table[addr]);
    }
    // Final CRC15 is shifted left by 1 so the on-wire LSB is always 0.
    return (uint16_t)((remainder << 1) & 0xFFFEu);
}

void pec15_append(uint8_t *data, size_t len) {
    uint16_t pec = pec15_compute(data, len);
    data[len]     = (uint8_t)((pec >> 8) & 0xFFu);
    data[len + 1] = (uint8_t)(pec & 0xFFu);
}
