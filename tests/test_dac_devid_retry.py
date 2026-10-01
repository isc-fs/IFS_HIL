"""Guards on DEVID retry.

The read is occasionally MIS-CLOCKED under CAN load. A healthy DAC1 returned
0x082E while ~200 frames/s crossed the shared SPI bus, and 0x082E is 0x0417 << 1
-- the same five bits shifted one position. It corrects itself on the next
transaction. Reproduced in 2 minutes of sustained traffic; 15 back-to-back
flashes and 36 idle hours produced none (IFS_HIL#124).

Reading once let one bad transaction fail a whole run and, via the watchdog,
trigger a rail power-on-reset -- for a one-bit glitch that had already gone.
"""
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import bench   # noqa: E402

GOOD = bench.DAC_DEVID_EXPECTED
SHIFTED = (GOOD << 1) & 0xFFFF          # 0x082E, the value actually observed


def _probe_with(monkeypatch, sequences):
    """Drive probe() with a scripted DEVID response per DAC index."""
    seqs = {i: list(v) for i, v in sequences.items()}

    def fake_try(client, method, **kw):
        if method == "dac.read_device_id":
            q = seqs.get(kw["idx"], [GOOD])
            return q.pop(0) if q else GOOD
        if method.endswith("is_present"):
            return False
        if method == "psu.status":
            return {"ps_on": True, "pwr_ok": True}
        return None

    monkeypatch.setattr(bench, "_client", lambda: object())
    monkeypatch.setattr(bench, "_try", fake_try)
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)
    return bench.probe(deep=False)


def test_the_observed_glitch_is_a_one_bit_shift():
    """Documents what this is defending against: same bits, moved one place."""
    assert SHIFTED == 0x082E
    assert bin(GOOD).count("1") == bin(SHIFTED).count("1") == 5


def test_a_transient_misread_does_not_condemn_the_bench(monkeypatch):
    """The whole point. One bad read then a good one is a healthy DAC."""
    r = _probe_with(monkeypatch, {1: [SHIFTED, GOOD]})
    assert r["dac_bad_devid"] == [], "a self-correcting glitch must not fail the bench"
    assert r["dac_devid"]["1"] == f"0x{GOOD:04X}"


def test_a_genuinely_dead_dac_is_still_reported(monkeypatch):
    """Retry must not become a way to ignore a real failure."""
    r = _probe_with(monkeypatch, {2: [0x0000, 0x0000, 0x0000, 0x0000]})
    assert 2 in r["dac_bad_devid"]


def test_the_retry_is_recorded_not_swallowed(monkeypatch):
    """A retry rate that climbs is the same fault getting worse. Absorbing it
    silently would hide exactly the signal worth watching."""
    r = _probe_with(monkeypatch, {1: [SHIFTED, GOOD]})
    rt = r["dac_devid_retries"]["1"]
    assert rt["attempts"] == 2
    assert rt["recovered"] is True
    assert f"0x{SHIFTED:04X}" in rt["seen"], "the bad value must be kept for diagnosis"
    assert any("retry" in w.lower() for w in r["warnings"])


def test_retries_are_bounded(monkeypatch):
    """A dead DAC must not spin: bounded attempts, then a verdict."""
    r = _probe_with(monkeypatch, {0: [SHIFTED] * 20})
    assert r["dac_devid_retries"]["0"]["attempts"] == bench.DAC_DEVID_RETRIES
    assert 0 in r["dac_bad_devid"]
