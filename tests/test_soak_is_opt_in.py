"""Guards on `soak`: the long and destructive AMS cases run only when asked for.

`pyproject.toml` registered `soak` as "opt-in via `pytest -m soak`", and Block F
says its rows are marked "so the default suite stays fast". Nothing enforced
either -- no `addopts`, no hook -- so a plain `pytest tests/hil/ams/` collected
all 19 soak cases. `configs/suites.yaml` maps the AMS `full` suite to that
directory, which put them in every `/hil-test full`, unattended. One of them is
F-077: it cuts carrier power mid-flash, ten times over, and an interrupted flash
has left an STM32H7 unrecoverable over CAN before
(isc-fs/stm32-can-bootloader#166).

Block G's E-050/E-051/E-052 were not marked at all: about 72 minutes of soak
against the test job's 90-minute timeout, and a timeout kills the job wherever
it is -- mid-flash included.

These collect for real, in a subprocess, under the repo's own pytest
configuration: the defect was a missing default, and only a real collection
sees defaults.
"""
import functools
import itertools
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AMS = "tests/hil/ams/"

F077 = ("tests/hil/ams/test_block_f_flash_endurance.py::"
        "TestF077InterruptedFlashRecovery::test_f077_interrupted_flash_recovery")

# 30 min, 30 min and 50 power cycles.
LONG_BLOCK_G = (
    "tests/hil/ams/test_block_g_soak.py::TestE050IdleSoakInStart::test_e050",
    "tests/hil/ams/test_block_g_soak.py::TestE051RunSoak::test_e051",
    "tests/hil/ams/test_block_g_soak.py::TestE052PowerCycleResilience::test_e052",
)

# Same file as E-050..E-052, but seconds long -- they must stay in.
SHORT_BLOCK_G = (
    "tests/hil/ams/test_block_g_soak.py::TestG097BootDiag::test_g097_boot_diag",
    "tests/hil/ams/test_block_g_soak.py::"
    "TestG102PerIcPecLocalisation::test_g102_pec_localisation",
)


@functools.lru_cache(maxsize=None)
def _collect(*args):
    """Node ids a run of `tests/hil/ams/` would execute with `args` added.

    The command CI runs, minus the bench: `--collect-only` imports every module
    and applies marker selection without executing anything. PYTEST_ADDOPTS is
    dropped so this sees the repo's default, not the caller's shell.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", AMS, "--collect-only", "-q",
         "-p", "no:cacheprovider", "--no-kpi", *args],
        cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, (
        f"collecting {AMS} {' '.join(args)} failed (exit {r.returncode}):\n"
        f"{r.stdout}{r.stderr}")
    # `-q` prints one node id per line, then a blank line, then the summary.
    return frozenset(itertools.takewhile(bool, r.stdout.splitlines()))


def test_no_soak_case_runs_by_default():
    soak = _collect("-m", "soak")
    assert soak, "nothing collects under -m soak, so this guard checks nothing"
    leaked = sorted(_collect() & soak)
    assert not leaked, (
        f"{len(leaked)} soak case(s) in a default run of {AMS} -- keep "
        "`-m 'not soak'` in pyproject.toml's addopts:\n  " + "\n  ".join(leaked))


def test_f077_interrupted_flash_never_runs_by_default():
    """Named on its own, so a rename or a lost mark fails here instead of
    passing quietly because the id no longer matches anything."""
    assert F077 in _collect("-m", "soak"), (
        f"{F077} does not run under -m soak -- renamed, or lost its mark?")
    assert F077 not in _collect(), "F-077 cuts power mid-flash; it must never run unasked"


def test_the_long_block_g_soaks_are_opt_in():
    """Opt-in, not unreachable: `-m` is last-wins, so an explicit `-m soak`
    replaces the default `-m 'not soak'`."""
    soak, default = _collect("-m", "soak"), _collect()
    for case in LONG_BLOCK_G:
        assert case in soak, f"{case} does not run under -m soak -- unmarked?"
        assert case not in default, f"{case} would run unasked"


def test_the_default_run_drops_only_the_soak_cases():
    """A module-wide mark on test_block_g_soak.py would have taken G-097 and
    G-102 out with the soaks."""
    everything = _collect("-m", "")          # an empty -m clears the filter
    default = _collect()
    assert default == everything - _collect("-m", "soak")
    for case in SHORT_BLOCK_G:
        assert case in default, f"{case} fell out of the default run"
