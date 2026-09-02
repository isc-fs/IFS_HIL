"""Guards that a dispatched run tests the firmware it flashed.

The A-003 case in each DUT's boot block REFLASHES the carrier. Its source path
comes from ECU_FIRMWARE_BIN / AMS_FIRMWARE_BIN, and those used to fall back to a
hardcoded local file. On bench-01 the ECU fallback existed -- a 2026-06-21
diagnostic build -- so a dispatched run flashed the PR image, then A-003 flashed
the June binary over it at 7 %, and the remaining 56 cases reported on that. It
passed while doing it.

The AMS only escaped because its fallback path happened not to exist, so its
A-003 skipped. Luck, not design -- which is exactly why this is pinned here.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "hil-test.yml"


def _flash_step():
    d = yaml.safe_load(WF.read_text())
    steps = d["jobs"]["test"]["steps"]
    return [s for s in steps if s.get("name", "").startswith("Flash and run")][0]["run"]


@pytest.mark.parametrize("dut,var", [("ecu", "ECU_FIRMWARE_BIN"),
                                     ("ams", "AMS_FIRMWARE_BIN")])
def test_the_run_pins_the_reflash_fixture_to_the_flashed_image(dut, var):
    run = _flash_step()
    assert f"export {var}=" in run, f"{var} is never exported; A-003 would reflash a stale binary"


def test_the_pin_uses_an_absolute_path():
    """pytest need not run from the workspace root, and the artifact path is
    relative -- a relative pin would resolve differently or not at all."""
    run = _flash_step()
    assert "readlink -f" in run


def test_the_pin_happens_after_the_flash_and_before_pytest():
    run = _flash_step()
    flash_at = run.index("tools.flash_dut")
    pin_at = run.index("ECU_FIRMWARE_BIN")
    pytest_at = run.index("-m pytest")
    assert flash_at < pin_at < pytest_at


@pytest.mark.parametrize("path,var", [
    ("tests/hil/vcu/test_block_a_boot.py", "ECU_FIRMWARE_BIN"),
    ("tests/hil/ams/test_block_a_boot.py", "AMS_FIRMWARE_BIN"),
])
def test_the_fixture_refuses_to_fall_back_silently_in_ci(path, var):
    """Off-bench convenience is fine; a dispatched run silently testing some
    other binary is not. In CI an unset variable must fail, not fall back."""
    src = (ROOT / path).read_text()
    fx = src[src.index(var):]
    fx = fx[:fx.index("return p")]
    assert "GITHUB_ACTIONS" in fx
    assert "pytest.fail" in fx
    # the fail must come BEFORE the fallback path is used
    assert fx.index("pytest.fail") < fx.index("is_file")
