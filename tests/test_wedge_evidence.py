"""Guards on wedge evidence capture.

Automatic recovery and diagnosis are in tension: a rail power-on-reset is
exactly what makes the DACs answer again, so every recovery erases the state
that would explain why they stopped (IFS_HIL#124). With the watchdog running
every 5 minutes, a wedge can come and go overnight leaving nothing behind.
"""
import argparse
import inspect
import json
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import bench   # noqa: E402


def test_evidence_is_captured_before_recovery_runs():
    """The whole point. Recovering first destroys what we came to look at."""
    src = inspect.getsource(bench.cmd_watchdog)
    assert "capture_wedge_evidence" in src
    assert src.index("capture_wedge_evidence") < src.index("recover(level)"), \
        "capture must precede the recovery ladder"


def test_capture_failure_does_not_block_recovery():
    """Diagnosis is worth less than a working bench. If capture throws, the
    ladder must still run."""
    src = inspect.getsource(bench.cmd_watchdog)
    seg = src[src.index("capture_wedge_evidence"):src.index("for level in")]
    assert "except" in seg and "continuing to recover" in seg


def test_devids_are_sampled_repeatedly(monkeypatch, tmp_path):
    """bench.py's own note says the floating-low and floating-high patterns have
    been seen ALTERNATING between reads. One sample cannot show that."""
    monkeypatch.setattr(bench, "WEDGE_LOG", str(tmp_path / "w.jsonl"))
    monkeypatch.setattr(bench, "_client", lambda: object())
    monkeypatch.setattr(bench, "_try", lambda c, m, **k: 0x0417 if "device_id" in m else {"pwr_ok": True})
    ev = bench.capture_wedge_evidence("bench-01", "test", samples=4)
    assert len(ev["dac_devid_samples"]) == 4
    assert all(set(row) == {"0", "1", "2", "3"} for row in ev["dac_devid_samples"])


def test_the_rail_state_is_recorded(monkeypatch, tmp_path):
    """`pwr_ok` low would mean the DACs are simply unpowered rather than
    mis-driven -- it settles the open question in one field."""
    monkeypatch.setattr(bench, "WEDGE_LOG", str(tmp_path / "w.jsonl"))
    monkeypatch.setattr(bench, "_client", lambda: object())
    monkeypatch.setattr(bench, "_try",
                        lambda c, m, **k: {"ps_on": True, "pwr_ok": False} if m == "psu.status" else 0x0000)
    ev = bench.capture_wedge_evidence("bench-01", "test", samples=2)
    assert ev["psu"] == {"ps_on": True, "pwr_ok": False}


def test_episodes_accumulate_rather_than_overwrite(monkeypatch, tmp_path):
    """Wedges are intermittent; a log that keeps only the last one cannot show
    a pattern."""
    log = tmp_path / "w.jsonl"
    monkeypatch.setattr(bench, "WEDGE_LOG", str(log))
    monkeypatch.setattr(bench, "_client", lambda: object())
    monkeypatch.setattr(bench, "_try", lambda c, m, **k: 0x0000)
    bench.capture_wedge_evidence("bench-01", "first", samples=1)
    bench.capture_wedge_evidence("bench-01", "second", samples=1)
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert [r["reason"] for r in rows] == ["first", "second"]


def test_a_missing_broker_is_recorded_not_raised(monkeypatch, tmp_path):
    """A wedged bench may also have a sick broker. Capture must degrade, not
    throw, or it takes recovery down with it."""
    monkeypatch.setattr(bench, "WEDGE_LOG", str(tmp_path / "w.jsonl"))
    def boom():
        # The REAL failure mode: _client() calls sys.exit() when the broker
        # socket is missing. SystemExit is a BaseException, so an `except
        # Exception` does not catch it -- this test used to raise RuntimeError
        # and passed against code that would have died on a real bench.
        sys.exit("no broker socket at /run/hil-broker/broker.sock")
    monkeypatch.setattr(bench, "_client", boom)
    ev = bench.capture_wedge_evidence("bench-01", "test", samples=2)
    assert "error" in ev and "no broker socket" in ev["error"]
