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
# SDC stim — bench-side `DIGITAL1` (= `SLOT1_DIG1` ↔ `IOEXP2_P10`) drive
# ---------------------------------------------------------------------------

@pytest.fixture
def sdc_closed(ams_profile, mlc_powered):
    """Drive `DIGITAL1` HIGH on the carrier so the AMS firmware reads SDC
    as closed.

    This is a bench wiring detail: the carrier's `SLOT1_DIG1` net (= MCU
    PE9) is jumpered to TCA9555 U7 P10 on the backplane. The TCA9555 sits
    on standby power so it's safe to drive without first powering the
    carrier, but we still depend on `mlc_powered` so the test ordering
    matches the rest of Block A.

    Use this fixture for tests that should observe a healthy SDC. Tests
    that need to verify SDC-open behaviour (HIL-019) should NOT request
    this fixture; their conftest entry-point can drive the pin LOW
    explicitly."""
    from broker.server import BrokerClient
    import os
    client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                         "/run/hil-broker/broker.sock"))
    addr = int(ams_profile["sdc_tca_addr"])
    port = int(ams_profile["sdc_tca_port"])
    pin  = int(ams_profile["sdc_tca_pin"])

    # Configure the whole port as outputs (mask=0x00) so the pin actually
    # drives; the other pins on the port retain whatever state the
    # `tca.write_pin` calls have left them in.
    client.call("tca.set_direction", addr=addr, port=port, mask=0x00)
    client.call("tca.write_pin",     addr=addr, port=port, pin=pin, value=True)
    log.info("SDC closed via TCA 0x%02X port %d pin %d", addr, port, pin)
    yield {"addr": addr, "port": port, "pin": pin}

    # Teardown — open SDC, leave the carrier in the safest state.
    client.call("tca.write_pin", addr=addr, port=port, pin=pin, value=False)
    client.close()


# ---------------------------------------------------------------------------
# ACU heartbeat — periodic 0x100 DC-bus frame so the VCU staleness
# predicate doesn't immediately fault after the chip jumps to the app.
# ---------------------------------------------------------------------------

@pytest.fixture
def acu_heartbeat(ams_profile, mlc_powered):
    """Run a background thread that emits the VCU DC-bus frame at
    `acu_heartbeat_period_ms` cadence (default 50 ms = 20 Hz, well under
    the firmware's `kVcuStaleMs` = 200 ms).

    The thread stops cleanly on teardown. Coexists with the `acu` fixture
    (separate `AcuStim` instance, separate SocketCAN socket) so a single
    test can do one-shot ACU stimulation on top of a steady heartbeat."""
    import threading
    from tools.firmware_test.acu_stim import AcuStim

    bus = ams_profile["bus_acu"]
    _skip_if_no_can(bus)

    period_s = float(ams_profile["acu_heartbeat_period_ms"]) / 1000.0
    volts    = int(ams_profile["acu_heartbeat_dc_bus_v"])

    stim = AcuStim(channel=bus)
    stim.start()
    stop_evt = threading.Event()

    def _loop():
        # Emit one frame, sleep, repeat. `emit_dc_bus_v_periodic` would do
        # the same but its arg shape (callable + period + event) is
        # awkward when we want a constant value; inline is clearer.
        while not stop_evt.is_set():
            try:
                stim.send_dc_bus_v(volts)
            except Exception as e:
                log.warning("acu_heartbeat send failed: %s", e)
                break
            stop_evt.wait(period_s)

    t = threading.Thread(target=_loop, name="acu-heartbeat", daemon=True)
    t.start()
    log.info("ACU heartbeat running on %s @ %d ms (%d V)",
             bus, int(period_s * 1000), volts)

    yield {"channel": bus, "period_ms": int(period_s * 1000), "volts": volts}

    stop_evt.set()
    t.join(timeout=1.0)
    stim.stop()


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
        discover_timeout_ms=int(ams_profile["bl_discover_timeout_ms"]),
        per_frame_timeout_ms=int(ams_profile["bl_per_frame_timeout_ms"]),
    )


# ---------------------------------------------------------------------------
# FSM state helper — used by every Block C test
# ---------------------------------------------------------------------------

@pytest.fixture
def wait_for_state(observe_acu, ams_profile):
    """Returns a function `wait(expected_state, timeout_ms)` that polls the
    latest `0x4A0` telemetry frame until byte 0 == expected_state, or the
    timeout expires.

    Returns the decoded payload on success; raises AssertionError on timeout
    with a diagnostic that includes the last state actually seen."""
    import time
    from tools.firmware_test.ams import can_map as M

    def _wait(expected_state: int, timeout_ms: int | None = None,
              poll_interval_s: float = 0.05) -> dict:
        timeout_ms = timeout_ms or int(ams_profile["state_transition_window_ms"])
        deadline = time.time() + timeout_ms / 1000.0
        last_decoded = None
        while time.time() < deadline:
            frame = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if frame is not None:
                last_decoded = M.decode_telem_status(frame.data)
                if last_decoded["state"] == expected_state:
                    return last_decoded
            time.sleep(poll_interval_s)
        raise AssertionError(
            f"FSM did not reach {M.FsmState.name(expected_state)} within "
            f"{timeout_ms} ms. Last observed: "
            f"{last_decoded['state_name'] if last_decoded else '(no 0x4A0 yet)'}"
        )

    return _wait


@pytest.fixture
def current_state(observe_acu):
    """Returns a function `current_state()` that reads the latest FSM state
    from the most recent `0x4A0` frame, or None if none seen yet."""
    from tools.firmware_test.ams import can_map as M

    def _read() -> int | None:
        frame = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
        if frame is None:
            return None
        return M.decode_telem_status(frame.data)["state"]

    return _read
