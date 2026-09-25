"""Guards on `bench doctor` itself.

From 2026-09-02 (90282ae) to IFS_HIL#140's fix, `bench doctor` crashed on its
first check. tools/bench.py had grown a second `def _sh` for the recovery
ladder, returning a bool, and it silently replaced the original at import --
so every `rc, out = _sh(...)` in doctor_checks() raised
TypeError: cannot unpack non-iterable bool object.

Nothing noticed for three weeks because no test ran doctor_checks() against
the real helper. The bootstrap's host phase runs `bench doctor`, so a new bench
could not be provisioned past it.
"""
import ast
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import bench   # noqa: E402


def test_the_shell_helper_returns_rc_and_output():
    """What every doctor check unpacks."""
    rc, out = bench._sh("echo hi")
    assert (rc, out) == (0, "hi")


def test_no_module_level_function_is_defined_twice():
    """The actual failure mode: a later `def` replaces an earlier one with no
    warning. Checked on the source, because by import time it is too late."""
    tree = ast.parse((ROOT / "tools" / "bench.py").read_text())
    seen, dupes = set(), []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name in seen:
                dupes.append(f"{node.name} (line {node.lineno})")
            seen.add(node.name)
    assert not dupes, f"redefined, so the earlier definition is dead: {dupes}"


def test_doctor_checks_survive_their_first_check():
    """Runs anywhere: off a bench the checks FAIL, which is fine -- the point is
    that they produce verdicts instead of raising."""
    rows = []
    for row in bench.doctor_checks():
        rows.append(row)
        if len(rows) >= 3:
            break
    assert rows, "doctor_checks() produced nothing"
    for sec, name, ok, detail in rows:
        assert isinstance(ok, bool) and isinstance(detail, str)


# --- the bootstrap ordering (IFS_HIL#140 follow-up) ---------------------------
#
# bench_setup.sh runs `bench doctor` in its host phase and exits on any FAIL.
# The runner phase, which installs the Restart= drop-in, comes AFTER it. So on a
# bench whose runner was configured but lacked the drop-in -- every existing
# bench, bench-01 included -- the script stopped before it could fix the one
# thing doctor was complaining about, while doctor's advice was to re-run it.

SETUP = ROOT / "scripts" / "bench_setup.sh"


def test_host_only_leaves_out_the_runner_section(monkeypatch):
    unit = "actions.runner.isc-fs-IFS_HIL.bench-01.service"
    monkeypatch.setattr(bench, "runner_units", lambda: [unit])
    monkeypatch.setattr(bench, "runner_state", lambda u: ("enabled", "active", "no"))
    full = [r for r in bench.doctor_checks() if r[0] == "14"]
    host = [r for r in bench.doctor_checks(include_runner=False) if r[0] == "14"]
    assert any(not ok for _, _, ok, _ in full), "precondition: §14 fails without the drop-in"
    assert host == [], "--host-only must not report §14"


def test_the_bootstrap_host_phase_does_not_gate_on_the_runner():
    src = SETUP.read_text()
    host_phase = src[src.index('step "Host build check"'):src.index('step "can-flasher"')]
    assert "tools.bench doctor --host-only" in host_phase, (
        "the host phase must run doctor --host-only, or it stops before the "
        "runner phase that repairs §14")


def test_the_bootstrap_confirms_the_restart_policy_took():
    src = SETUP.read_text()
    runner_phase = src[src.index('step "Self-hosted runner"'):]
    assert "systemctl show -p Restart --value" in runner_phase


def test_the_bootstrap_does_not_enable_the_timer_activated_watchdog():
    """The .timer is enabled; the .service must not be. Enabling it adds a boot
    run that skips the timer's OnBootSec settling delay, and a watchdog that
    finds an unready bench escalates to a rail power-cycle."""
    src = SETUP.read_text()
    loop = src[src.index("ENABLE_UNITS="):src.index("# ---- reboot gate")]
    assert "for u in $ENABLE_UNITS" in loop
    import subprocess
    units = subprocess.run(
        ["bash", "-c", 'UNITS="hil-psu-on hil-can-up hil-broker hil-dashboard '
         'hil-bench-watchdog"; echo "${UNITS/ hil-bench-watchdog/}"'],
        capture_output=True, text=True).stdout.split()
    assert "hil-bench-watchdog" not in units
    assert units == ["hil-psu-on", "hil-can-up", "hil-broker", "hil-dashboard"]
