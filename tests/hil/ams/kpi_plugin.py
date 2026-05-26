"""
AMS HIL KPI plugin — per-session ledger of bench-hours, cycles, and
test-outcome stats. Aggregated across sessions by
`scripts/hil_kpi_report.py` into a multi-session summary.

Design goal: quantify "hours of testing this methodology produces"
in a way that survives across sessions and survives the bench
moving / Pi being reflashed. Each session emits one JSON file under
`.kpi/<session_id>.json` next to the repo root; the aggregator
scoops them all up.

Counters are bumped from inside the fixtures that perform the action
they measure (power_cycle_count from `mlc_powered` / `fresh_boot`,
flash_cycle_count from `flasher`, bl_trigger_count from any test that
sends `0x002`). The plugin itself only tracks pytest-side numbers
(time, outcomes, frame counts). Counter shim functions are exposed
at module scope so fixtures can `kpi_plugin.bump_power_cycle()`
without needing to thread a state dict everywhere.

KPI categories per `docs/ams-hil/test-plan-v1.5.0.md` §4.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Counter shim — fixtures bump these via module-scope functions so they
# don't have to know about the plugin's internal state object.
# ---------------------------------------------------------------------------

_STATE: dict[str, Any] = {
    "power_cycle_count":     0,
    "flash_cycle_count":     0,
    "bl_trigger_count":      0,
    "frames_observed_total": 0,
    "block_f_cycles_completed": 0,
    # Per-test fine-grained timing buckets, for active-time aggregation.
    "per_test_durations_s":  {},   # nodeid -> wall-clock seconds
    "per_test_outcomes":     {},   # nodeid -> "passed" / "failed" / etc
    # BL DISCOVER latency samples (ms) — captured by anything that calls
    # can-flasher and parses the per-cycle latency.
    "bl_discover_latencies_ms": [],
}


def bump_power_cycle(n: int = 1) -> None:
    _STATE["power_cycle_count"] += n


def bump_flash_cycle(n: int = 1) -> None:
    _STATE["flash_cycle_count"] += n


def bump_bl_trigger(n: int = 1) -> None:
    _STATE["bl_trigger_count"] += n


def bump_frames_observed(n: int) -> None:
    _STATE["frames_observed_total"] += n


def bump_block_f_cycle(n: int = 1) -> None:
    _STATE["block_f_cycles_completed"] += n


def record_bl_discover_latency_ms(latency_ms: float) -> None:
    _STATE["bl_discover_latencies_ms"].append(float(latency_ms))


# ---------------------------------------------------------------------------
# Session state — populated by pytest hooks.
# ---------------------------------------------------------------------------

@dataclass
class SessionKpi:
    session_id:                 str
    hostname:                   str
    bench:                      str
    started_at_unix:            float
    started_at_iso:             str
    git_sha:                    str
    git_dirty:                  bool
    pytest_args:                list[str]
    finished_at_unix:           float = 0.0
    session_wall_clock_s:       float = 0.0

    # Per-outcome counts (from pytest collection + reports).
    tests_collected:            int = 0
    tests_executed:             int = 0
    tests_passed:               int = 0
    tests_failed:               int = 0
    tests_errored:              int = 0
    tests_skipped:              int = 0
    tests_xfailed:              int = 0
    tests_xpassed:              int = 0
    tests_deselected:           int = 0

    # Sum of per-test wall-clock — the "active testing" time.
    total_test_time_s:          float = 0.0
    bench_utilisation_pct:      float = 0.0

    # Bench-event counters (bumped via the module-level shims above).
    power_cycle_count:          int = 0
    flash_cycle_count:          int = 0
    bl_trigger_count:           int = 0
    frames_observed_total:      int = 0
    block_f_cycles_completed:   int = 0

    # BL DISCOVER timing (ms).
    bl_discover_latency_ms_mean:   float | None = None
    bl_discover_latency_ms_p50:    float | None = None
    bl_discover_latency_ms_p99:    float | None = None

    # Equivalent operator-hours heuristic (see docs §4).
    equivalent_operator_hours:  float = 0.0

    # Failure roster — nodeids of every test that didn't pass. Used
    # by the cross-session aggregator to detect regressions and
    # flakes.
    failed_tests:               list[str] = field(default_factory=list)
    errored_tests:              list[str] = field(default_factory=list)


_SESSION: SessionKpi | None = None


# ---------------------------------------------------------------------------
# CLI option
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    group = parser.getgroup("hil-kpi", "AMS HIL KPI ledger")
    group.addoption(
        "--kpi-dir",
        action="store",
        default=".kpi",
        help="Directory to drop per-session KPI JSON files into "
             "(default: .kpi/ in cwd). Disable by passing --no-kpi.",
    )
    group.addoption(
        "--no-kpi",
        action="store_true",
        default=False,
        help="Disable the KPI plugin for this session.",
    )
    group.addoption(
        "--bench-name",
        action="store",
        default=os.environ.get("HIL_BENCH_NAME", "default"),
        help="Bench identifier baked into the ledger (default: 'default' "
             "or $HIL_BENCH_NAME). Helps multi-bench aggregation.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_status() -> tuple[str, bool]:
    """Return (short_sha, dirty). Falls back to ('unknown', False) if
    git isn't available (e.g. on the bench Pi's non-git checkout)."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=2).strip()
    except Exception:
        return ("unknown", False)
    try:
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL, text=True, timeout=2).strip())
    except Exception:
        dirty = False
    return (sha, dirty)


def _percentile(samples: list[float], pct: float) -> float | None:
    """Inclusive percentile, classic. None for empty input."""
    if not samples:
        return None
    s = sorted(samples)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _operator_hours_heuristic(state: SessionKpi) -> float:
    # Per-action seconds a human would spend on the same workload.
    # Deliberately conservative for the human; see docs §4 for the model.
    return (state.power_cycle_count * 30
            + state.flash_cycle_count * 60
            + state.bl_trigger_count * 45
            # FSM transitions are observed implicitly via tests_executed,
            # using executed-tests as a rough proxy for "transitions a
            # human would have eyeballed". Tunable.
            + state.tests_executed * 30) / 3600.0


# ---------------------------------------------------------------------------
# Pytest hooks
# ---------------------------------------------------------------------------

def pytest_sessionstart(session):
    if session.config.getoption("--no-kpi"):
        return
    sha, dirty = _git_status()
    global _SESSION
    _SESSION = SessionKpi(
        session_id     = uuid.uuid4().hex[:12],
        hostname       = socket.gethostname(),
        bench          = session.config.getoption("--bench-name"),
        started_at_unix= time.time(),
        started_at_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        git_sha        = sha,
        git_dirty      = dirty,
        pytest_args    = list(session.config.invocation_params.args),
    )


def pytest_collection_modifyitems(config, items):
    if _SESSION is None:
        return
    _SESSION.tests_collected = len(items)


def pytest_deselected(items):
    if _SESSION is None:
        return
    _SESSION.tests_deselected += len(items)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if _SESSION is None:
        return
    rep = outcome.get_result()
    # Only count the actual call phase (not setup/teardown) for
    # active-test time, but use the worst outcome of any phase.
    if rep.when == "call":
        _SESSION.total_test_time_s += rep.duration
        _STATE["per_test_durations_s"][rep.nodeid] = rep.duration
        _STATE["per_test_outcomes"][rep.nodeid] = rep.outcome
    if rep.when in ("call", "setup", "teardown") and rep.failed and rep.when != "call":
        # Errored in setup or teardown -> classify as errored.
        if rep.nodeid not in _SESSION.errored_tests:
            _SESSION.errored_tests.append(rep.nodeid)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _SESSION is None:
        return
    stats = terminalreporter.stats
    _SESSION.tests_passed   = len(stats.get("passed",   []))
    _SESSION.tests_failed   = len(stats.get("failed",   []))
    _SESSION.tests_errored  = len(stats.get("error",    []))
    _SESSION.tests_skipped  = len(stats.get("skipped",  []))
    _SESSION.tests_xfailed  = len(stats.get("xfailed",  []))
    _SESSION.tests_xpassed  = len(stats.get("xpassed",  []))
    _SESSION.tests_executed = (_SESSION.tests_passed
                                + _SESSION.tests_failed
                                + _SESSION.tests_errored)

    for rep in stats.get("failed", []):
        if hasattr(rep, "nodeid") and rep.nodeid not in _SESSION.failed_tests:
            _SESSION.failed_tests.append(rep.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if _SESSION is None:
        return
    _SESSION.finished_at_unix = time.time()
    _SESSION.session_wall_clock_s = (
        _SESSION.finished_at_unix - _SESSION.started_at_unix)

    # Roll in shim counters.
    _SESSION.power_cycle_count        = _STATE["power_cycle_count"]
    _SESSION.flash_cycle_count        = _STATE["flash_cycle_count"]
    _SESSION.bl_trigger_count         = _STATE["bl_trigger_count"]
    _SESSION.frames_observed_total    = _STATE["frames_observed_total"]
    _SESSION.block_f_cycles_completed = _STATE["block_f_cycles_completed"]

    # Bench utilisation = active test time as a % of wall-clock.
    if _SESSION.session_wall_clock_s > 0:
        _SESSION.bench_utilisation_pct = (
            _SESSION.total_test_time_s
            / _SESSION.session_wall_clock_s * 100.0)

    # BL DISCOVER stats.
    lat = _STATE["bl_discover_latencies_ms"]
    if lat:
        _SESSION.bl_discover_latency_ms_mean = sum(lat) / len(lat)
        _SESSION.bl_discover_latency_ms_p50  = _percentile(lat, 50)
        _SESSION.bl_discover_latency_ms_p99  = _percentile(lat, 99)

    # Operator-hours heuristic.
    _SESSION.equivalent_operator_hours = _operator_hours_heuristic(_SESSION)

    # Write the ledger.
    kpi_dir = Path(session.config.getoption("--kpi-dir"))
    kpi_dir.mkdir(parents=True, exist_ok=True)
    path = kpi_dir / f"{_SESSION.session_id}.json"
    payload = asdict(_SESSION)
    # Tag platform info for cross-host aggregation.
    payload["platform"] = {
        "system":  platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python":  platform.python_version(),
    }
    with path.open("w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Inline fixture-friendly hooks (importable by other conftest fixtures
# without having to traverse pytest's plugin registry).
# ---------------------------------------------------------------------------

__all__ = [
    "bump_power_cycle",
    "bump_flash_cycle",
    "bump_bl_trigger",
    "bump_frames_observed",
    "bump_block_f_cycle",
    "record_bl_discover_latency_ms",
]
