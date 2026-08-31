"""Guards on the Pico emulator's NTC AUX table.

The emulator has to emit the AUX voltage the AMS decoder expects. It used to
invert a beta model with two constants the AMS firmware no longer has -- a
10 kOhm series resistor and beta 3380 -- so every seeded temperature read 3-10
degC high (IFS_HIL#117): 25 C came back as 34 C. These pin the emitted table to
the divider the firmware actually documents.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "tools" / "pico_ltc_emulator" / "firmware" / "src"

# ams_config.hpp:1027-1028 — the BMS_LITE divider.
NTC_PULLUP_OHM = 6800
NTC_VREF_MV = 3000
NTC_R25_OHM = 10000


def _table():
    s = (SRC / "ntc_aux_table.h").read_text()
    body = s.split("{", 1)[1].split("}", 1)[0]
    vals = [int(x) for x in re.findall(r"(\d+)\s*,", body)]
    tmin = int(re.search(r"NTC_AUX_T_MIN_C\s+\((-?\d+)\)", s).group(1))
    return tmin, vals


def test_table_is_contiguous_and_covers_the_operating_range():
    tmin, vals = _table()
    assert tmin == -55
    assert len(vals) == 181            # -55..125 inclusive
    assert all(0 < v <= 30000 for v in vals)


def test_aux_falls_monotonically_with_temperature():
    """An NTC's resistance drops as it heats, so the divider node falls too. A
    non-monotonic table would make the AMS's inverse lookup ambiguous."""
    _, vals = _table()
    assert all(b < a for a, b in zip(vals, vals[1:]))


def test_25C_sits_exactly_at_the_R25_divider_point():
    """The one point that needs no curve at all: at 25 C the thermistor is R25
    by definition, so V = VREF * R25/(Rpullup + R25) = 1785.7 mV. The old model
    emitted 1500 mV here -- a 10k pull-up instead of 6.8k -- and the AMS read
    that back as 34 C. This is the single assertion that would have caught it."""
    tmin, vals = _table()
    aux_25 = vals[25 - tmin] / 10.0          # 100-uV units -> mV
    expect = NTC_VREF_MV * NTC_R25_OHM / (NTC_PULLUP_OHM + NTC_R25_OHM)
    assert abs(aux_25 - expect) < 1.0, f"{aux_25} mV vs {expect:.1f} mV"


def test_round_trip_through_the_ams_decoder_returns_the_seeded_resistance():
    """Emit V(T), invert it the way the AMS does, and the resistance must land
    on the manufacturer curve -- monotonic and passing through R25 at 25 C."""
    tmin, vals = _table()
    prev = None
    for t in range(-40, 121, 5):
        v = vals[t - tmin] / 10.0
        r = NTC_PULLUP_OHM * v / (NTC_VREF_MV - v)
        if t == 25:
            assert abs(r - NTC_R25_OHM) < 10, f"R at 25 C = {r:.1f}"
        if prev is not None:
            assert r < prev, f"resistance must fall with temperature at {t} C"
        prev = r


def test_the_beta_model_is_gone_from_the_emulator():
    """3380 is a Murata NCP15XH103J; the fitted part is a Fenghua
    CMFB103F3950. If a beta fit reappears, so does the calibration error."""
    src = (SRC / "ltc6811_emu.c").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))
    assert "expf" not in code
    assert "3380" not in code
    assert "k_ntc_aux_100uV" in code
