"""Fleet-inventory guards for configs/benches/.

Runs anywhere — no hardware, no broker. These are the checks that keep the
descriptors trustworthy as benches are added by people who did not write the
schema: a malformed or dishonest descriptor routes somebody else's test run to
a bench that cannot do the job, and the failure surfaces an hour later on the
wrong hardware.
"""

import json

import pytest

yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

from tools.bench import (BENCH_DIR, SCHEMA_PATH, load_descriptors,
                         runner_labels)


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def descriptors():
    return load_descriptors()


def test_at_least_one_bench_declared(descriptors):
    assert descriptors, f"no bench descriptors found in {BENCH_DIR}"


def test_every_descriptor_matches_schema(schema, descriptors):
    validator = jsonschema.Draft202012Validator(schema)
    for bench_id, (path, desc) in descriptors.items():
        errors = sorted(validator.iter_errors(desc), key=lambda e: list(e.path))
        assert not errors, "{}: {}".format(
            path.name, "; ".join(f"{e.json_path}: {e.message}" for e in errors))


def test_id_matches_filename(descriptors):
    """The filename is how a human finds a bench. If it drifts from the id,
    editing bench-02.yaml silently reconfigures bench-03."""
    for bench_id, (path, _) in descriptors.items():
        assert path.stem == bench_id, f"{path.name} declares id '{bench_id}'"


def test_bench_ids_are_unique(descriptors):
    """load_descriptors() keys by id, so a duplicate would silently shadow one
    bench. Count files instead to catch it."""
    assert len(descriptors) == len(list(BENCH_DIR.glob("*.yaml")))


def test_capabilities_never_name_hardware(schema):
    """The vocabulary routes test runs, so it must describe what a bench can DO.
    A hardware name here couples every test to one implementation: swap the
    emulator and each request naming `pico-ltc` breaks for no real reason.
    """
    allowed = schema["properties"]["capabilities"]["items"]["enum"]
    families = ("dut-", "stim-", "fault-", "radio-")
    for cap in allowed:
        assert cap.startswith(families), (
            f"'{cap}' is outside the capability families {families}")
    for banned in ("pico", "ltc6811", "dac80504", "interposer", "tca", "ina"):
        assert not any(banned in cap for cap in allowed), (
            f"'{banned}' names hardware; capabilities must name the capability")


def test_declared_slot_duts_are_canonical(descriptors):
    """ams / ecu / udv, settled to end the vcu-vs-ecu-vs-ECU08 drift."""
    for bench_id, (_, desc) in descriptors.items():
        for slot, spec in (desc.get("slots") or {}).items():
            assert spec["dut"] in ("ams", "ecu", "udv", "none"), (
                f"{bench_id} slot {slot}: unknown dut '{spec['dut']}'")


def test_dut_capability_agrees_with_slots(descriptors):
    """A bench claiming dut-ams must actually seat an AMS. This is the cheapest
    place to catch a copy-pasted descriptor."""
    for bench_id, (_, desc) in descriptors.items():
        seated = {s["dut"] for s in (desc.get("slots") or {}).values()}
        for cap in desc["capabilities"]:
            if cap.startswith("dut-"):
                dut = cap[len("dut-"):]
                assert dut in seated, (
                    f"{bench_id} claims {cap} but no slot declares dut: {dut}")


def test_runner_labels_include_id_and_capabilities(descriptors):
    """Routing matches on labels, so every capability must reach the runner."""
    for bench_id, (_, desc) in descriptors.items():
        labels = runner_labels(desc)
        assert bench_id in labels
        assert "self-hosted" in labels
        for cap in desc["capabilities"]:
            assert cap in labels


def test_can_sample_point_is_declared(descriptors):
    """A silent sample-point mismatch presents as an unexplained bus-off rather
    than a config error, so every bus must state the value preflight checks."""
    for bench_id, (_, desc) in descriptors.items():
        for role, spec in desc["can"].items():
            assert "sample_point" in spec, (
                f"{bench_id} can[{role}] does not declare sample_point")


def test_hosts_are_the_only_place_addresses_live(descriptors):
    """Guards the phase-1 rule: bench addresses live in the descriptor (and the
    CLAUDE.md table), never scattered through scripts."""
    for bench_id, (_, desc) in descriptors.items():
        assert desc["hosts"], f"{bench_id} declares no host"
        for kind, target in desc["hosts"].items():
            assert "@" in target, f"{bench_id} hosts.{kind} is not user@host"
