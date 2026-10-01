"""Guards on the AMS-view -> LTC-chain cell mapping.

Every per-cell fault injection in tests/hil/ams goes through this map. It was
inverted (10/9 instead of 9/10) against ams_config.hpp, which sent
`inject_cell_v(m, 10, ...)` to module cell 9, made cell 9 address a channel the
AMS never reads, and left cell 18 unreachable. Undervoltage tests still passed
-- the AMS trips whichever neighbour goes low -- so nothing caught it. These
pin the mapping to the firmware constants instead.
"""
import pytest

pytest.importorskip("serial")

from tools.pico_ltc_emulator.host.pico_ltc_client import (   # noqa: E402
    CELLS_PER_LTC_LOWER, CELLS_PER_LTC_UPPER, CELLS_PER_MODULE, PicoLtcClient)

m2c = PicoLtcClient._module_cell_to_chain


def test_split_matches_ams_config():
    """ams_config.hpp:993-994 — upper 9, lower 10. Measured on bench-01 too:
    upper chain positions answer 9 cells, lower ones 10."""
    assert (CELLS_PER_LTC_UPPER, CELLS_PER_LTC_LOWER) == (9, 10)
    assert CELLS_PER_MODULE == 19


def test_upper_ltc_carries_cells_0_to_8():
    for m in range(5):
        for c in range(9):
            assert m2c(m, c) == (2 * m, c)


def test_lower_ltc_carries_cells_9_to_18():
    for m in range(5):
        for c in range(9, 19):
            assert m2c(m, c) == (2 * m + 1, c - 9)


def test_every_module_cell_maps_somewhere_the_ams_reads():
    """95 distinct (chain, offset) pairs, none past a real channel: the upper
    LTC has 9 and the lower 10, so an offset of 9 is only valid on a lower."""
    seen = set()
    for m in range(5):
        for c in range(19):
            pos, off = m2c(m, c)
            assert (pos, off) not in seen, f"module {m} cell {c} collides"
            seen.add((pos, off))
            limit = CELLS_PER_LTC_UPPER if pos % 2 == 0 else CELLS_PER_LTC_LOWER
            assert off < limit, f"module {m} cell {c} -> chain {pos} offset {off}, past {limit}"
    assert len(seen) == 95


def test_the_boundary_cell_is_the_first_of_the_lower_ltc():
    """The specific off-by-one that was wrong: cell 9 is LTC_2 offset 0, not
    LTC_1 offset 9 (which the AMS does not read)."""
    assert m2c(0, 8) == (0, 8)
    assert m2c(0, 9) == (1, 0)
    assert m2c(0, 18) == (1, 9)
