"""Guards for the DUT flasher's resolution and safety logic.

Runs anywhere — no hardware, no broker. These cover the decisions that are
dangerous to get wrong: which slot a DUT is in, which OTHER carriers must be
dark before flashing, and which boot trigger identifies which DUT.
"""

import pytest

yaml = pytest.importorskip("yaml")

from tools.flash_dut import (PROFILE_FOR_DUT, FlashError, carrier_power,
                             other_dut_slots, resolve)


def test_resolves_each_dut_to_its_own_slot():
    _, _, ams = resolve("ams", "bench-01")
    _, _, ecu = resolve("ecu", "bench-01")
    assert ams == 2 and ecu == 4
    assert ams != ecu


def test_refuses_a_dut_the_bench_does_not_declare():
    """Routing already prevents this, but the flasher must not rely on routing
    having been honest — it is the last check before energising a carrier."""
    with pytest.raises((FlashError, KeyError)):
        resolve("udv", "bench-01")


def test_every_other_dut_slot_is_de_energised():
    """THE safety property. Both bootloaders answer node 0x01, so a second
    powered carrier means `discover` can reply from the wrong board and the
    wrong firmware gets written."""
    desc, _, slot = resolve("ams", "bench-01")
    others = other_dut_slots(desc, slot)
    assert (4, "ecu") in others, "the ECU carrier must be dropped before flashing the AMS"
    assert all(s != slot for s, _ in others)


def test_empty_slots_are_not_touched():
    """Slots declaring dut: none hold nothing; cycling their relays would be
    pointless actuation of bench hardware."""
    desc, _, slot = resolve("ams", "bench-01")
    duts = {d for _, d in other_dut_slots(desc, slot)}
    assert "none" not in duts and None not in duts


def test_the_two_duts_have_different_boot_triggers():
    """They share bl_node_id 0x01 and are told apart ONLY by this payload.
    flash_helper.py hardcodes the AMS one, which is why it lives in the profile."""
    payloads = {}
    for dut in ("ams", "ecu"):
        _, profile, _ = resolve(dut, "bench-01")
        assert "bl_trigger_payload" in profile, f"{dut} profile declares no boot trigger"
        payloads[dut] = profile["bl_trigger_payload"]
    assert payloads["ams"] != payloads["ecu"], (
        f"both DUTs would answer the same trigger: {payloads}")


def test_flash_parameters_come_from_the_profile_not_the_code():
    for dut in ("ams", "ecu"):
        _, profile, _ = resolve(dut, "bench-01")
        for key in ("bl_node_id", "app_flash_address", "bl_discover_timeout_ms"):
            assert key in profile, f"{dut} profile is missing {key}"


def test_carrier_power_is_read_from_the_descriptor():
    desc, _, _ = resolve("ams", "bench-01")
    tca, port = carrier_power(desc)
    assert (tca, port) == (0x20, 0)


def test_every_known_dut_has_a_profile_file():
    from pathlib import Path
    from tools.flash_dut import REPO_ROOT
    for dut, rel in PROFILE_FOR_DUT.items():
        assert (REPO_ROOT / rel).is_file(), f"{dut} points at a missing profile: {rel}"


def test_each_dut_declares_the_product_its_bootloader_reports():
    """The identity gate. bl_node_id is provisioned into flash separately from
    the firmware constant and demonstrably drifts — bench-01's AMS carrier
    answers 0x01 while ams_config.hpp declares AmsNodeId = 0x02. The product
    string is what the board says it IS, so it catches a mis-slotted or
    mis-provisioned carrier that a node-id check would wave through."""
    products = {}
    for dut in ("ams", "ecu"):
        _, profile, _ = resolve(dut, "bench-01")
        assert "bl_product" in profile, f"{dut} profile declares no bl_product"
        products[dut] = profile["bl_product"]
    assert products["ams"] != products["ecu"]
    assert products["ams"] == "IFS08-CE-AMS"
    assert products["ecu"] == "IFS08-CE-ECU"


def test_isolation_is_the_default_not_restore():
    """A run started from the ECU repo must exercise the ECU alone. Restoring
    the relay snapshot after flashing would re-energise the AMS immediately and
    put a second node on the bus for the whole suite, so isolation is the
    default and restore is opt-in."""
    import inspect

    from tools.flash_dut import flash
    sig = inspect.signature(flash)
    assert sig.parameters["restore_relays"].default is False, (
        "restore_relays must default to False — isolation is the safe default")


def test_a_build_recipe_exists_for_every_flashable_dut():
    """The recipe is IFS_HIL-owned and reviewed here: a PR must not be able to
    change how its own firmware is built when the result is written to shared
    hardware. Every DUT the flasher can target needs one."""
    import yaml as _yaml
    from tools.flash_dut import PROFILE_FOR_DUT, REPO_ROOT
    for dut in PROFILE_FOR_DUT:
        r = REPO_ROOT / "configs" / "firmware" / f"{dut}.yaml"
        assert r.is_file(), f"no build recipe for {dut}: {r}"
        rec = _yaml.safe_load(r.read_text())
        for key in ("dut", "repo", "configure", "build", "elf", "layout_check"):
            assert key in rec, f"{r.name} is missing {key}"
        assert rec["dut"] == dut


def test_the_recipes_are_the_same_shape():
    """isc-fs/IFS08-CE-ECU#235 moved the ECU firmware to the repo root, so both
    recipes are now command-for-command identical apart from the output name.
    Before that the ECU root was project(ECU08_NSIL_Tests) and building "the
    repo" produced a HOST binary that would have been flashed to a carrier.

    If this ever diverges again, the divergence is the thing to look at, not
    the recipe."""
    import yaml as _yaml
    from tools.flash_dut import REPO_ROOT
    recs = {d: _yaml.safe_load((REPO_ROOT / f"configs/firmware/{d}.yaml").read_text())
            for d in ("ams", "ecu")}
    for d, r in recs.items():
        assert r["configure"] == (
            "cmake -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake"), (
            f"{d} no longer builds the AMS way: {r['configure']}")
        assert r["build"] == "cmake --build build -j"
        assert r["elf"].startswith("build/")
    assert recs["ams"]["elf"] == "build/AMS.elf"
    assert recs["ecu"]["elf"] == "build/ECU08.elf"
