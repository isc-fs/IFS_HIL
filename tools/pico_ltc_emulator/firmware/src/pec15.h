// PEC15 — CRC-15 used by Linear Tech / ADI LTC68xx-family chips.
// Polynomial 0xC599 (x^15 + x^14 + x^10 + x^8 + x^7 + x^4 + x^3 + 1),
// per LTC6811 datasheet section "PEC Calculation".

#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Compute PEC15 over `len` bytes starting at `data`. Returns the
// 15-bit CRC left-shifted by 1 to land in a 16-bit word with bit 0
// always 0 (matches the on-wire encoding the LTC68xx transmits as
// the 2-byte PEC trailer, MSB first).
uint16_t pec15_compute(const uint8_t *data, size_t len);

// Append the 2-byte PEC15 trailer to a buffer in-place. `data[len..len+1]`
// must be writable. After return, `data[0..len+1]` is the wire-format
// payload + PEC trailer (MSB first at data[len]).
void pec15_append(uint8_t *data, size_t len);

#ifdef __cplusplus
}
#endif
