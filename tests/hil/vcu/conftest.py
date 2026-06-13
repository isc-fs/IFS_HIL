"""
VCU / ECU HIL test fixtures (IFS08-CE-ECU#35). The VCU is on MLC4.

Layered on `tests/hil/conftest.py`'s `broker_available` + `psu_on`; mirrors
`tests/hil/ams/conftest.py`. Wiring lives in `vcu_profile.yaml`. The pedal /
inverter-inject / pit-diag fixtures land with their blocks (C/D/E/F/G); this
file carries the foundation + Block B's needs.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest
import yaml

from tools.firmware_test.vcu import can_map as M

log = logging.getLogger(__name__)
PROFILE_PATH = Path(__file__).parent / "vcu_profile.yaml"


def _broker():
    from broker.server import BrokerClient
    return BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                       "/run/hil-broker/broker.sock"))


@pytest.fixture(scope="session")
def vcu_profile() -> dict:
    with PROFILE_PATH.open() as f:
        return yaml.safe_load(f)


def _skip_if_no_can(channel: str) -> None:
    if not Path(f"/sys/class/net/{channel}").exists():
        pytest.skip(f"SocketCAN interface {channel} not present")


# -- carrier power (MLC4) --------------------------------------------
def _robust_power_on(client, relay_bit, ina_addr, min_mA, observe=None,
                     *, settle_s=0.3, retries=4):
    """Close K_n and VERIFY the carrier drew current across two samples;
    re-toggle until solidly powered. The bench TCA/relay contact is
    intermittent — same helper as the AMS conftest."""
    mA = 0.0
    for attempt in range(1, retries + 1):
        if observe is not None:
            observe.clear()
        client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=True)
        t_on = time.monotonic()
        ok = True
        for _ in range(2):
            time.sleep(settle_s)
            try:
                mA = client.call("ina.current", addr=ina_addr) * 1000.0
            except Exception:
                mA = 0.0
            if mA < min_mA:
                ok = False
                break
        if ok:
            if attempt > 1:
                log.warning("VCU carrier powered only on attempt %d (%.0f mA)",
                            attempt, mA)
            return t_on
        client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
        time.sleep(0.4)
    pytest.fail(f"MLC4 never drew >= {min_mA:.0f} mA after {retries} relay "
                f"closes (last {mA:.0f} mA) -- bench/relay, not firmware.")


@pytest.fixture(scope="session")
def mlc_powered(broker_available, psu_on, vcu_profile):
    client = _broker()
    slot = int(vcu_profile["mlc_slot"])
    relay_bit = slot - 1
    ina_addr = {1: 0x40, 2: 0x41, 3: 0x44, 4: 0x45}[slot]
    client.call("tca.set_direction", addr=0x20, port=0, mask=0x00)
    _robust_power_on(client, relay_bit, ina_addr,
                     float(vcu_profile["mlc_boot_current_mA"]))
    log.info("MLC%d (VCU) powered", slot)
    yield {"slot": slot, "relay_bit": relay_bit, "ina_addr": ina_addr}
    client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
    client.close()


# -- observers (INV = can0, ACU = can2) ------------------------------
@pytest.fixture
def observe_acu(vcu_profile, mlc_powered):
    """Sniffer on the ACU/flash bus (FDCAN2, kernel can2) — 0x100, pit-diag."""
    from tools.firmware_test.can_observer import CanObserver
    bus = vcu_profile["bus_acu"]
    _skip_if_no_can(bus)
    with CanObserver(channel=bus) as obs:
        yield obs


@pytest.fixture
def observe_inv(vcu_profile, mlc_powered):
    """Sniffer on the INV bus (FDCAN1, kernel can0) — 0x360/0x362, 0x461-0x466."""
    from tools.firmware_test.can_observer import CanObserver
    bus = vcu_profile["bus_inv"]
    _skip_if_no_can(bus)
    with CanObserver(channel=bus) as obs:
        yield obs


# -- fresh_boot: power-cycle MLC4, wait for the VCU 0x100 heartbeat ---
@pytest.fixture
def fresh_boot(vcu_profile, mlc_powered, observe_acu):
    """Power-cycle the VCU carrier and return once the first `0x100` heartbeat
    arrives. The VCU emits 0x100 in *all* states from boot, so no stimulus is
    needed for liveness (FSM-transition stimulus comes with the C-block)."""
    client = _broker()
    relay_bit = mlc_powered["relay_bit"]
    ext = bool(vcu_profile.get("heartbeat_extended", True))
    try:
        client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
        time.sleep(2.0)
        t_on = _robust_power_on(client, relay_bit, mlc_powered["ina_addr"],
                                float(vcu_profile["mlc_boot_current_mA"]),
                                observe_acu)
    finally:
        client.close()

    deadline = time.monotonic() + 6.0
    first = None
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_HEARTBEAT, extended=ext)
        if f is not None:
            first = f
            break
        time.sleep(0.02)
    if first is None:
        pytest.fail(
            f"No 0x100 heartbeat within 6 s of VCU power-on (extended={ext}). "
            "App didn't reach CanTxTask, or wrong bus / id-format."
        )
    return {
        "first_frame":  M.decode_heartbeat(first.data),
        "t_power_on":   t_on,
        "t_first_frame": time.monotonic(),
    }
