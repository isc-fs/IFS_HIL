"""Guards on the standing bench watchdog.

HIL runs are unattended. A bench that wedges at 02:00 stays wedged until someone
notices, and bench-01's DACs wedge often enough (IFS_HIL#124) that "someone
notices" is not a plan. hil-test.yml's preflight also recovers, but only when a
run happens to start -- too late for the developer who triggered it, and never
for an idle bench.

The property that makes a standing watchdog safe rather than dangerous is that
it NEVER acts while the bench is busy: level 2 recovery power-cycles the rails,
and doing that mid-flash is the interrupted write that leaves an H7
unrecoverable (F-077).
"""
import argparse
import fcntl
import multiprocessing
import sys
import time
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import bench   # noqa: E402


def _hold_lock(path, seconds, ready):
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        ready.set()
        time.sleep(seconds)


@pytest.fixture
def unhealthy(monkeypatch, tmp_path):
    """A bench that always fails its descriptor, and a recover() that only
    records that it was asked."""
    calls = []
    monkeypatch.setattr(bench, "BENCH_LOCK", str(tmp_path / "bench.lock"))
    monkeypatch.setattr(bench, "_verify_quiet", lambda b: (False, "dac 0 bad"))
    monkeypatch.setattr(bench, "recover", lambda lvl: calls.append(lvl))
    return calls


def test_it_does_not_touch_a_busy_bench(unhealthy, tmp_path):
    """The one that matters. A held lock means a run is mid-flash or mid-suite;
    the watchdog must return immediately without recovering."""
    ready = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_lock, args=(bench.BENCH_LOCK, 10, ready))
    holder.start()
    try:
        assert ready.wait(5), "lock holder did not start"
        t0 = time.monotonic()
        rc = bench.cmd_watchdog(argparse.Namespace(bench="bench-01", verbose=False))
        elapsed = time.monotonic() - t0
        assert rc == 0, "a busy bench is not a watchdog failure"
        assert unhealthy == [], "must NOT recover while the bench is busy"
        assert elapsed < 5, f"must not block waiting for the lock (took {elapsed:.1f}s)"
    finally:
        holder.terminate(); holder.join()


def test_it_escalates_when_the_bench_is_free(unhealthy, monkeypatch):
    """Free bench, still unhealthy after both rungs -> non-zero so systemd
    records the failure, having tried level 1 then level 2."""
    rc = bench.cmd_watchdog(argparse.Namespace(bench="bench-01", verbose=False))
    assert unhealthy == [1, 2]
    assert rc != 0


def test_it_stops_at_the_first_rung_that_works(monkeypatch, tmp_path):
    """A rail power-cycle disturbs every carrier on the bench, so it must not
    happen when a broker restart was enough."""
    calls = []
    state = {"healthy": False}
    monkeypatch.setattr(bench, "BENCH_LOCK", str(tmp_path / "bench.lock"))
    monkeypatch.setattr(bench, "_verify_quiet", lambda b: (state["healthy"], ""))
    def fake_recover(lvl):
        calls.append(lvl)
        state["healthy"] = True          # level 1 fixes it
    monkeypatch.setattr(bench, "recover", fake_recover)
    rc = bench.cmd_watchdog(argparse.Namespace(bench="bench-01", verbose=False))
    assert calls == [1], "must not escalate to a power cycle after level 1 worked"
    assert rc == 0


def test_a_healthy_bench_is_a_no_op(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(bench, "BENCH_LOCK", str(tmp_path / "bench.lock"))
    monkeypatch.setattr(bench, "_verify_quiet", lambda b: (True, ""))
    monkeypatch.setattr(bench, "recover", lambda lvl: calls.append(lvl))
    assert bench.cmd_watchdog(argparse.Namespace(bench="bench-01", verbose=True)) == 0
    assert calls == []


def test_the_timer_is_shipped_and_reasonable():
    unit = (ROOT / "infra" / "systemd" / "hil-bench-watchdog.timer").read_text()
    assert "OnUnitActiveSec=5min" in unit
    assert "OnBootSec" in unit, "a bench that came up wrong should say so promptly"
    assert "RandomizedDelaySec" in unit, "a fleet must not power-cycle in lockstep"
