"""Guards on automatic bench recovery.

The DAC80504s wedge -- five separate times in one day -- and the cure is always
the same escalating ritual. Doing it by hand meant a red verdict on a PR whose
firmware was fine, so preflight now does it. These pin the two properties that
make that safe rather than reckless.
"""
import inspect
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from tools import bench   # noqa: E402

WF = yaml.safe_load((ROOT / ".github" / "workflows" / "hil-test.yml").read_text())
PREFLIGHT = [s for s in WF["jobs"]["test"]["steps"]
             if "Preflight" in (s.get("name") or "")][0]["run"]


def test_recovery_takes_the_bench_lock_before_touching_anything():
    """Level 2 power-cycles the rails. Doing that while another run is mid-flash
    is the interrupted write that leaves an H7 unrecoverable (F-077). The lock
    must be acquired in the command itself, so a hand-run recovery is protected
    too -- not left to whoever remembers to wrap it."""
    src = inspect.getsource(bench.cmd_recover)
    assert "flock" in src and "LOCK_EX" in src
    assert src.index("flock") < src.index("recover(args.level)"), \
        "the lock must be held BEFORE any recovery step runs"


def test_level_2_rebuilds_can_after_the_power_cycle():
    """A PSU cycle resets the MCP2515s while the kernel still believes the
    interfaces are up: CAN goes silent with the link reading UP/ERROR-ACTIVE.
    That looks exactly like a dead DUT and has been misdiagnosed as a brick."""
    src = inspect.getsource(bench.recover)
    assert "modprobe" in src and "mcp251x" in src
    assert src.index("_psu_cycle") < src.index("modprobe"), \
        "CAN must be rebuilt AFTER the power cycle that knocked it over"


def test_a_power_cycle_is_not_the_first_thing_tried():
    """Cheapest rung first: a broker restart is ~6 s and often enough, a rail
    POR is ~27 s and disturbs every carrier on the bench."""
    src = inspect.getsource(bench.recover)
    assert "level <= 1" in src
    assert src.index("hil-broker") < src.index("_psu_cycle")


def test_preflight_escalates_then_still_fails_if_the_bench_is_unfit():
    """Recovery must not become a way to paper over a genuinely broken bench:
    if it still does not match its descriptor, the run stops."""
    assert "bench recover" in PREFLIGHT
    assert "--level" in PREFLIGHT
    assert "exit 1" in PREFLIGHT, "an unrecoverable bench must fail the run"
    assert "::error::" in PREFLIGHT


def test_preflight_says_when_recovery_was_needed():
    """A bench that silently self-heals every run hides a worsening fault. The
    warning is what makes the pattern visible in the logs."""
    assert "needed recovery level" in PREFLIGHT
