"""
AMS HIL test fixtures.

Layered on top of `tests/hil/conftest.py`, which provides `broker_available`
and `psu_on`. Here we add:
  - `ams_profile`     — loaded YAML thresholds & cadences
  - `mlc_powered`     — energises the carrier slot, waits for BL boot
  - `bms_emulator`    — runs BmsEmulator on FDCAN2 for the test duration
  - `acu`             — AcuStim on FDCAN1
  - `observe_acu`     — CanObserver on FDCAN1
  - `observe_bms`     — CanObserver on FDCAN2
  - `flasher`         — CanFlasher pre-pointed at FDCAN2

Many of these need real hardware; they call `pytest.skip` on missing pieces
so an off-bench `pytest tests/` stays clean.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

import pytest
import yaml

from tools import hw_config as CFG
from tools.hil_client import psu_status                       # noqa: F401

log = logging.getLogger(__name__)

PROFILE_PATH = Path(__file__).parent / "ams_profile.yaml"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def ams_profile() -> dict:
    """Tunable test thresholds, loaded once per session."""
    with PROFILE_PATH.open() as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Carrier power
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def mlc_powered(broker_available, psu_on, ams_profile):
    """Energise the AMS carrier slot, wait for the chip to boot, hand back to
    the test. De-energises on session teardown."""
    from broker.server import BrokerClient
    import os
    client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                         "/run/hil-broker/broker.sock"))

    slot = int(ams_profile["mlc_slot"])
    if slot not in (1, 2, 3, 4):
        pytest.fail(f"ams_profile.mlc_slot must be 1..4, got {slot}")

    relay_bit = slot - 1                              # K1=bit0, K2=bit1, ...
    ina_addr  = {1: 0x40, 2: 0x41, 3: 0x44, 4: 0x45}[slot]

    client.call("tca.set_direction", addr=0x20, port=0, mask=0x00)
    client.call("tca.write_pin",     addr=0x20, port=0, pin=relay_bit, value=True)
    time.sleep(float(ams_profile["mlc_boot_settle_s"]))

    current_mA = client.call("ina.current", addr=ina_addr) * 1000
    min_mA = float(ams_profile["mlc_boot_current_mA"])
    if current_mA < min_mA:
        client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
        pytest.fail(
            f"MLC{slot} drew only {current_mA:.1f} mA after K{slot} close "
            f"(< {min_mA:.0f} mA expected). Check carrier seating and fuse."
        )

    log.info("MLC%d powered, %.1f mA", slot, current_mA)
    yield {"slot": slot, "current_mA": current_mA}

    # teardown — drop the relay
    client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
    client.close()


# ---------------------------------------------------------------------------
# CAN bus helpers
# ---------------------------------------------------------------------------

def _skip_if_no_can(channel: str) -> None:
    if not Path(f"/sys/class/net/{channel}").exists():
        pytest.skip(f"SocketCAN interface {channel} not present")


@pytest.fixture
def acu(ams_profile, mlc_powered):
    """ACU-bus stimulus (FDCAN1, kernel `can0`)."""
    from tools.firmware_test.acu_stim import AcuStim
    bus = ams_profile["bus_acu"]
    _skip_if_no_can(bus)
    with AcuStim(channel=bus) as a:
        yield a


@pytest.fixture
def bms_emulator(ams_profile, mlc_powered):
    """BMS slave emulator on FDCAN2. Defaults to a healthy pack
    (cells at 3700 mV, temps at 25 °C)."""
    from tools.firmware_test.ams.bms_emulator import BmsEmulator
    bus = ams_profile["bus_bms_bl"]
    _skip_if_no_can(bus)
    with BmsEmulator(channel=bus) as emu:
        emu.set_all_cells(int(ams_profile["bms_default_cell_mV"]))
        emu.set_all_temps(int(ams_profile["bms_default_temp_C"]))
        yield emu


@pytest.fixture
def observe_acu(ams_profile, mlc_powered):
    """Passive CAN sniffer on the ACU bus."""
    from tools.firmware_test.can_observer import CanObserver
    bus = ams_profile["bus_acu"]
    _skip_if_no_can(bus)
    with CanObserver(channel=bus) as obs:
        yield obs


@pytest.fixture
def observe_bms(ams_profile, mlc_powered):
    """Passive CAN sniffer on the BMS/BL bus."""
    from tools.firmware_test.can_observer import CanObserver
    bus = ams_profile["bus_bms_bl"]
    _skip_if_no_can(bus)
    with CanObserver(channel=bus) as obs:
        yield obs


# ---------------------------------------------------------------------------
# Flasher
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def flasher(ams_profile, mlc_powered):
    """`can-flasher` wrapper bound to the BL bus."""
    from tools.firmware_test.flash_helper import CanFlasher
    if shutil.which("can-flasher") is None:
        pytest.skip("can-flasher binary not on PATH")
    return CanFlasher(
        channel=ams_profile["bus_bms_bl"],
        bitrate=500_000,
        node_id=int(ams_profile["bl_node_id"]),
        timeout_ms=int(ams_profile["bl_discover_timeout_ms"]),
    )
