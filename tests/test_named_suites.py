"""Guards on named test suites.

A developer labelling a PR should choose what runs, and should not be handed the
whole tree. The label used to run all 63 ECU cases -- including ones bench-01
structurally cannot pass (the E-blocks need an inverter it does not have; C-004
and L-005 need the AMS alive, but flash_dut de-energises MLC2 to flash the ECU)
-- so every PR came back red whatever it contained.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from tools.bench import load_suites, resolve_suite   # noqa: E402


def test_every_dut_defines_a_smoke_suite():
    """`smoke` is the default a bare label runs, so it must exist for each DUT."""
    for dut, suites in load_suites().items():
        assert "smoke" in suites, f"{dut} has no smoke suite"
        assert suites["smoke"], f"{dut}'s smoke suite is empty"


def test_every_target_exists_on_disk():
    """A suite naming a path that has been renamed away fails at pytest
    collection, on the bench, after the flash -- an expensive place to find out."""
    for dut, suites in load_suites().items():
        for name, targets in suites.items():
            for t in targets:
                assert (ROOT / t).exists(), f"{dut}/{name}: missing target {t}"


def test_an_empty_spec_means_smoke():
    for dut, suites in load_suites().items():
        assert resolve_suite(dut, "") == list(suites["smoke"])
        assert resolve_suite(dut, "   ") == list(suites["smoke"])


def test_a_path_is_passed_through_untouched():
    """Arbitrary paths and single cases must keep working -- naming a suite is a
    convenience, not a restriction."""
    assert resolve_suite("ecu", "tests/hil/vcu/test_block_c_fsm.py") == \
        ["tests/hil/vcu/test_block_c_fsm.py"]
    assert resolve_suite("ecu", "tests/hil/vcu/test_block_c_fsm.py::TestC001VdcConfigGate") == \
        ["tests/hil/vcu/test_block_c_fsm.py::TestC001VdcConfigGate"]


def test_an_unknown_name_is_an_error_not_a_silent_fallback():
    """A typo that quietly ran 63 cases instead of 20 is the surprise this
    exists to remove."""
    with pytest.raises(SystemExit) as e:
        resolve_suite("ecu", "smoek")
    assert "unknown suite" in str(e.value)
    assert "smoke" in str(e.value)          # names the alternatives


def test_smoke_excludes_what_the_bench_cannot_serve():
    """bench-01 declares no inverter, and flash_dut leaves the AMS dark during
    an ECU run. Those blocks belong in named suites, never in the default."""
    smoke = " ".join(resolve_suite("ecu", "smoke"))
    for cannot in ("test_block_e_inverter", "test_block_e_fault_recovery",
                   "test_block_l_dv"):
        assert cannot not in smoke, f"{cannot} cannot pass unattended; keep it out of smoke"


def test_smoke_is_narrower_than_full():
    for dut in load_suites():
        assert resolve_suite(dut, "smoke") != resolve_suite(dut, "full")


def test_the_suite_input_is_optional_on_every_trigger():
    """`smoke` is the default, so a caller wanting it must not have to name it.
    While `suite` was `required: true` a dispatch with an empty value was
    rejected outright -- HTTP 422 "Required input 'suite' not provided" -- which
    is how the firmware repo's bare label failed after it stopped forcing a path.
    """
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "hil-test.yml").read_text())
    on = wf[True]
    for trigger in ("workflow_dispatch", "workflow_call"):
        spec = on[trigger]["inputs"]["suite"]
        assert spec.get("required") is False, f"{trigger}: suite must be optional"
        assert spec.get("default", None) == "", f"{trigger}: suite must default to empty"
