"""Reference PEC15 implementation in Python + parity tests against the
known LTC6811 datasheet example. Used to:

  (a) sanity-check our understanding of the algorithm before flashing
      the firmware, and
  (b) verify reads coming back from the Pico over a loopback SPI
      master.

The firmware C and this Python implementation must agree byte-for-byte.
"""
from __future__ import annotations

import pytest


CRC15_POLY = 0x4599


def pec15_compute(data: bytes) -> int:
    """Return PEC15 CRC of `data`, encoded as the 16-bit on-wire value
    (15-bit CRC left-shifted by 1, bit 0 always 0). Matches the
    pseudocode in the LTC6811 datasheet figure 27."""
    remainder = 16
    for b in data:
        addr = ((remainder >> 7) ^ b) & 0xFF
        # Compute crc_table[addr] inline (small enough that table caching
        # gives no benefit at this scale).
        entry = addr << 7
        for _ in range(8):
            if entry & 0x4000:
                entry = ((entry << 1) ^ CRC15_POLY) & 0xFFFF
            else:
                entry = (entry << 1) & 0xFFFF
        entry &= 0x7FFF
        remainder = ((remainder << 8) ^ entry) & 0xFFFF
    return (remainder << 1) & 0xFFFE


# ---------------------------------------------------------------------------
# Known-good vectors from the LTC6811 datasheet and field captures
# ---------------------------------------------------------------------------

# 0x0001 (WRCFGA) -> 0x3D6E is the canonical example from the LTC6811
# datasheet (figure 28). 0x0004 (RDCVA) -> 0x07C2 is pinned from this
# implementation so any future change is caught -- not independently
# datasheet-verified; the firmware C output is the authority once we
# capture a real-world chain readout.
@pytest.mark.parametrize("payload,expected_pec", [
    (bytes([0x00, 0x01]), 0x3D6E),                                # WRCFGA cmd
    (bytes([0x00, 0x04]), 0x07C2),                                # RDCVA cmd
])
def test_known_datasheet_vectors(payload, expected_pec):
    assert pec15_compute(payload) == expected_pec


def test_pec_is_deterministic_for_zero_input():
    """Empty input -> seed (16) shifted left by 1 = 0x0020."""
    assert pec15_compute(b"") == 0x0020


def test_pec_changes_with_input():
    """Single-bit input change must propagate through the CRC."""
    a = pec15_compute(b"\x00")
    b = pec15_compute(b"\x01")
    assert a != b


def test_pec_is_lsb_zero():
    """The on-wire encoding is the 15-bit CRC left-shifted by 1, so bit
    0 must always be 0 for any input."""
    for n in range(16):
        for seed in (0x00, 0xFF, 0xA5):
            payload = bytes([seed] * (n + 1))
            assert pec15_compute(payload) & 0x01 == 0


def test_pec_fits_16_bits():
    for n in range(32):
        for seed in (0x00, 0xFF, 0xA5, 0x5A):
            payload = bytes([seed] * (n + 1))
            v = pec15_compute(payload)
            assert 0 <= v <= 0xFFFE
            assert v & 0x01 == 0
