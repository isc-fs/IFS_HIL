"""Guards on what preflight says when a DAC wedges.

The check used to fire on ANY bad DAC and always blame `stim-pack-current`. When
DACs 0 and 1 wedged it therefore named pack current -- which rides DAC 3 and was
healthy -- and never mentioned that DAC 0, the ECU's brake and APPS driver, was
dead. Wrong in both directions, and a developer reads it as "an AMS thing, not
my problem" while their pedal injection is down.
"""
import inspect
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from tools import bench   # noqa: E402

SRC = inspect.getsource(bench.cmd_verify)


def _routing(bench_id="bench-01"):
    return bench.get_descriptor(bench_id).get("routing", {})


def _dac_users(routing):
    users = {}
    for name, spec in routing.items():
        if isinstance(spec, dict) and "dac" in spec:
            users.setdefault(int(spec["dac"]), set()).add(name)
    return users


def test_pack_current_is_only_blamed_when_its_own_dac_is_bad():
    """The specific bug: `if "stim-pack-current" in caps and any bad dac`."""
    assert "pack_dacs & bad_dacs" in SRC, \
        "pack current must be gated on the DAC it actually routes through"


def test_every_bad_dac_is_reported_by_index():
    """Naming only a capability hides which hardware is down. The index is what
    someone at the bench needs."""
    assert 'f"DAC {idx} does not return a valid device id' in SRC


def test_the_descriptor_knows_what_each_dac_drives():
    """A bad DAC should say what it takes down with it. Without a routing entry
    the message can only give a bare index."""
    users = _dac_users(_routing())
    assert 0 in users and "pedals" in users[0], \
        "DAC 0 drives the ECU pedals; routing must say so"
    assert 3 in users and "pack_current" in users[3]


def test_the_ecu_pedal_dac_is_not_the_pack_current_dac():
    """The whole reason the old message was wrong. If these ever coincide on a
    bench, the blame logic needs revisiting rather than silently coinciding."""
    users = _dac_users(_routing())
    pedal = {i for i, n in users.items() if "pedals" in n}
    pack = {i for i, n in users.items() if "pack_current" in n}
    assert pedal and pack and not (pedal & pack)


def test_an_unrouted_dac_says_so_rather_than_guessing():
    assert "nothing in `routing` names it" in SRC
