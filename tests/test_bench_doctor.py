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
