"""Pin the composed AMS profile to its pre-refactor values.

The bench-physical half of `ams_profile.yaml` moved into
`configs/benches/<id>.yaml`, and the `ams_profile` fixture now overlays the two
layers. That refactor is only safe if the values tests actually see are
byte-for-byte what they were before, so the historical values are frozen here.

If a value below has to change, it is because the *bench* changed -- move the
value in the descriptor, not the expectation. If bench-01 is ever rewired, this
file is the tripwire that says so out loud.

Runs anywhere: no hardware, no broker.
"""

import pytest

yaml = pytest.importorskip("yaml")

from tests.hil.ams.conftest import _bench_wiring
from tools.bench import load_bench, slot_for_dut

# Exactly as they appeared in ams_profile.yaml before the split
# (git show <pre-split>:tests/hil/ams/ams_profile.yaml).
HISTORICAL_BENCH_01 = {
    "mlc_slot": 2,
    "bus_acu": "can2",
    "bus_bms_bl": "can2",

    "pack_current_dac_idx": 3,
    "pack_current_dac_ch_p": 0,
    "pack_current_dac_ch_n": 1,
    "pack_current_cm_v": 1.44,

    "current_heartbeat_dac_idx": 3,
    "current_heartbeat_dac_channel": 0,

    "tsms_tca_addr": 0x21,
    "tsms_tca_port": 1,
    "tsms_tca_pin": 0,

    "dash_chg_tca_addr": 0x21,
    "dash_chg_tca_port": 1,
    "dash_chg_tca_pin": 1,

    "ams_ok_adc_idx": 2, "ams_ok_adc_channel": 0,
    "air_p_adc_idx": 2,  "air_p_adc_channel": 1,
    "air_n_adc_idx": 2,  "air_n_adc_channel": 2,
    "prech_adc_idx": 2,  "prech_adc_channel": 3,
}


@pytest.fixture(scope="module")
def bench():
    return load_bench("bench-01")


def test_wiring_matches_pre_refactor_values(bench):
    wiring = _bench_wiring(bench)
    for key, expected in HISTORICAL_BENCH_01.items():
        assert key in wiring, f"{key} disappeared in the profile split"
        assert wiring[key] == expected, (
            f"{key}: descriptor gives {wiring[key]!r}, "
            f"pre-refactor profile had {expected!r}")


def test_no_bench_physical_keys_left_in_the_profile():
    """The split is only meaningful if the values moved rather than being
    duplicated -- a key in both layers makes the winner depend on merge order."""
    from tests.hil.ams.conftest import PROFILE_PATH
    profile = yaml.safe_load(PROFILE_PATH.read_text())
    leaked = sorted(set(profile) & set(HISTORICAL_BENCH_01))
    assert not leaked, (
        f"bench-physical keys are back in ams_profile.yaml: {leaked}. "
        "They belong in configs/benches/.")


def test_profile_still_carries_the_bench_independent_half():
    """Guards the other direction: the refactor must not have deleted firmware
    facts or test expectations along with the wiring."""
    from tests.hil.ams.conftest import PROFILE_PATH
    profile = yaml.safe_load(PROFILE_PATH.read_text())
    for key in ("bl_node_id", "fw_declared_node_id", "app_flash_address", "stub_cell_mV",
                "boot_grace_ms", "vcu_stale_ms", "tx_telemetry_period_ms",
                "pack_current_mv_per_a", "pack_current_accuracy_tol_pct",
                "acu_rx_dc_bus_id", "relay_readback_digital_threshold_v"):
        assert key in profile, f"{key} went missing from ams_profile.yaml"


def test_slot_lookup_is_by_dut_not_hardcoded(bench):
    assert slot_for_dut(bench, "ams") == 2
    assert slot_for_dut(bench, "ecu") == 4
    with pytest.raises(KeyError):
        slot_for_dut(bench, "udv")      # not seated on bench-01


def test_wiring_omits_rather_than_nulls_undescribed_hardware():
    """A bench that does not describe a fixture must drop the key entirely, so
    the consuming fixture skips exactly as it did when the profile omitted it --
    rather than handing tests a None that fails deep inside a DAC write."""
    bare = {"id": "bench-xx", "can": {"acu": {"dev": "can1", "bitrate": 500000}},
            "slots": {"3": {"dut": "ams"}}, "routing": {}}
    wiring = _bench_wiring(bare)
    assert wiring["mlc_slot"] == 3
    assert wiring["bus_acu"] == "can1"
    assert wiring["bus_bms_bl"] == "can1"       # falls back to the ACU bus
    for key in ("pack_current_dac_idx", "tsms_tca_addr", "ams_ok_adc_idx"):
        assert key not in wiring
