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


@pytest.fixture(autouse=True)
def no_runner(monkeypatch):
    """Every case here is about the HARDWARE ladder unless it says otherwise.
    The watchdog also inspects the self-hosted runner through systemd, and the
    machine running pytest -- a laptop, a GitHub-hosted runner -- may or may not
    have such a unit. Pin it to "no runner" so these results cannot depend on
    the host."""
    monkeypatch.setattr(bench, "runner_units", lambda: [])


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


# --- the runner (IFS_HIL#140) -------------------------------------------------
#
# bench-01's runner exited on 2026-09-18 on a transient "registration deleted"
# from GitHub and nothing restarted it. For a week every dispatched run queued
# against it, while this watchdog -- which only verified the hardware -- printed
# "healthy" every five minutes.

RUNNER = "actions.runner.isc-fs-IFS_HIL.bench-01.service"


def _runner(monkeypatch, active):
    monkeypatch.setattr(bench, "runner_units", lambda: [RUNNER])
    monkeypatch.setattr(bench, "runner_state",
                        lambda u: ("enabled", active, "always"))


def test_a_down_runner_is_a_failure_even_on_healthy_hardware(monkeypatch, tmp_path, capsys):
    """The exact blind spot: hardware fine, runner dead. Must not read as healthy."""
    calls = []
    monkeypatch.setattr(bench, "BENCH_LOCK", str(tmp_path / "bench.lock"))
    monkeypatch.setattr(bench, "_verify_quiet", lambda b: (True, ""))
    monkeypatch.setattr(bench, "recover", lambda lvl: calls.append(lvl))
    _runner(monkeypatch, "inactive")
    rc = bench.cmd_watchdog(argparse.Namespace(bench="bench-01", verbose=True))
    out = capsys.readouterr().out
    assert rc != 0, "a dead runner makes the bench unreachable to CI; that is a failure"
    assert "RUNNER DOWN" in out and RUNNER in out


def test_a_down_runner_never_triggers_a_rail_cycle(monkeypatch, tmp_path):
    """Level 2 power-cycles every carrier on the bench. A runner fault is not a
    hardware fault and must never cause one -- restarting the runner is
    systemd's job, via the Restart=always drop-in."""
    calls = []
    monkeypatch.setattr(bench, "BENCH_LOCK", str(tmp_path / "bench.lock"))
    monkeypatch.setattr(bench, "_verify_quiet", lambda b: (True, ""))
    monkeypatch.setattr(bench, "recover", lambda lvl: calls.append(lvl))
    _runner(monkeypatch, "failed")
    bench.cmd_watchdog(argparse.Namespace(bench="bench-01", verbose=False))
    assert calls == [], "a runner fault must not trigger hardware recovery"


def test_a_live_runner_changes_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(bench, "BENCH_LOCK", str(tmp_path / "bench.lock"))
    monkeypatch.setattr(bench, "_verify_quiet", lambda b: (True, ""))
    monkeypatch.setattr(bench, "recover", lambda lvl: calls.append(lvl))
    _runner(monkeypatch, "active")
    assert bench.cmd_watchdog(argparse.Namespace(bench="bench-01", verbose=True)) == 0
    assert calls == []


def test_a_down_runner_does_not_stop_hardware_recovery(unhealthy, monkeypatch):
    """Both broken at once: the hardware ladder must still run in full."""
    _runner(monkeypatch, "inactive")
    rc = bench.cmd_watchdog(argparse.Namespace(bench="bench-01", verbose=False))
    assert unhealthy == [1, 2], "the runner check must not short-circuit the ladder"
    assert rc != 0


def test_the_runner_restart_drop_in_is_shipped():
    """Enabled answers "does it come back after a power cut?". Restart= answers
    "does it come back after it quits?". The stock svc.sh unit has no Restart=."""
    conf = (ROOT / "infra" / "systemd" / "actions.runner.restart.conf").read_text()
    assert "Restart=always" in conf
    assert "RestartSec=" in conf, "must not hammer GitHub if the registration is really gone"
    assert "StartLimitIntervalSec=0" in conf, "systemd must not give up during a network outage"
