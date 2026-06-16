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
    ext = bool(vcu_profile.get("heartbeat_extended", False))
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


# -- pedal driver: bench DAC1 -> ECU ADC3 (brake / APPS1 / APPS2) -----
@pytest.fixture
def pedals(vcu_profile, mlc_powered):
    """Drive the ECU's three analog pedal inputs via DAC1. Helpers take ECU-ADC
    raw counts (0..ecu_adc_full_scale): set_brake/set_apps1/set_apps2 + rest()."""
    client = _broker()
    idx = int(vcu_profile["pedal_dac_idx"])
    vref = float(vcu_profile["pedal_vref"])
    fs = float(vcu_profile["ecu_adc_full_scale"])
    ch = {"brake": int(vcu_profile["brake_dac_channel"]),
          "apps1": int(vcu_profile["apps1_dac_channel"]),
          "apps2": int(vcu_profile["apps2_dac_channel"])}

    def set_raw(which, raw):
        v = max(0.0, min(vref, float(raw) / fs * vref))
        client.call("dac.set_voltage", idx=idx, channel=ch[which], volts=v)

    state = {
        "set_raw":   set_raw,
        "set_brake": lambda raw: set_raw("brake", raw),
        "set_apps1": lambda raw: set_raw("apps1", raw),
        "set_apps2": lambda raw: set_raw("apps2", raw),
        "rest":      lambda: [set_raw(k, 0) for k in ch],
    }
    state["rest"]()
    yield state
    state["rest"]()
    client.close()


# -- inverter inject (can0): 0x461 inv_state + 0x466 vdc gate ---------
@pytest.fixture
def inv_heartbeat(vcu_profile):
    """Background inject of the inverter TX frames the VCU consumes on can0.
    0x461 (inv_state, default 0 = no FSM transition) streams continuously; 0x466
    (vdc gate) starts OFF so WAIT_INV_VDC_CONFIG holds until vdc_ready()."""
    import threading
    from tools.firmware_test.acu_stim import AcuStim
    bus = vcu_profile["bus_inv"]
    _skip_if_no_can(bus)
    st = {"inv_state": 0, "vdc": 0, "cnt": 0, "send_vdc": False, "run": True}
    stim = AcuStim(channel=bus)
    stim.start()

    def loop():
        while st["run"]:
            st["cnt"] = (st["cnt"] + 1) & 0x0F
            try:
                stim.send_raw(M.ID_INV_STATE2,
                              M.encode_inv_state2(st["inv_state"], st["cnt"]),
                              is_extended_id=False)
                if st["send_vdc"]:
                    stim.send_raw(M.ID_INV_STATE7,
                                  M.encode_inv_state7(st["vdc"], st["cnt"]),
                                  is_extended_id=False)
            except Exception:
                pass
            time.sleep(0.05)

    threading.Thread(target=loop, daemon=True).start()
    st["set_state"] = lambda n: st.update(inv_state=int(n))
    st["vdc_ready"] = lambda v=0: st.update(vdc=int(v), send_vdc=True)
    yield st
    st["run"] = False
    time.sleep(0.1)
    stim.stop()


# -- ACU inject (can2): 0x020 precharge + 0x4A0 AMS status -----------
@pytest.fixture
def acu_inject(vcu_profile):
    """Background inject on the ACU bus (can2): 0x020 precharge-ack
    (set_precharge 0/1) and 0x4A0 AMS status byte0 (set_ams_state n)."""
    import threading
    from tools.firmware_test.acu_stim import AcuStim
    bus = vcu_profile["bus_acu"]
    _skip_if_no_can(bus)
    st = {"prech": 0, "ams": 0, "send_prech": False, "send_ams": False, "run": True}
    stim = AcuStim(channel=bus)
    stim.start()

    def loop():
        while st["run"]:
            try:
                if st["send_prech"]:
                    stim.send_raw(int(vcu_profile["acu_precharge_id"]),
                                  bytes([st["prech"] & 0xFF] + [0] * 7),
                                  is_extended_id=False)
                if st["send_ams"]:
                    stim.send_raw(int(vcu_profile["ams_status_id"]),
                                  bytes([st["ams"] & 0xFF] + [0] * 7),
                                  is_extended_id=False)
            except Exception:
                pass
            time.sleep(0.05)

    threading.Thread(target=loop, daemon=True).start()
    st["set_precharge"] = lambda v: st.update(prech=int(v), send_prech=True)
    st["set_ams_state"] = lambda n: st.update(ams=int(n), send_ams=True)
    yield st
    st["run"] = False
    time.sleep(0.1)
    stim.stop()


# -- start button: bench ioexp3 (TCA 0x22) -> ECU PB5 ----------------
@pytest.fixture
def start_button(vcu_profile, mlc_powered):
    """Drive the ECU start button (PB5/D2) via TCA 0x22 P1.0. press()/release()."""
    client = _broker()
    addr = int(vcu_profile["start_btn_tca_addr"])
    port = int(vcu_profile["start_btn_tca_port"])
    pin = int(vcu_profile["start_btn_tca_pin"])
    client.call("tca.set_direction", addr=addr, port=port, mask=0x00)

    def drive(level):
        client.call("tca.write_pin", addr=addr, port=port, pin=pin, value=bool(level))

    drive(False)
    state = {"press": lambda: drive(True), "release": lambda: drive(False)}
    yield state
    drive(False)
    client.close()


# -- pit-diag: enable the 0x700-0x703 stream, read the FSM state -----
@pytest.fixture
def pit_diag(vcu_profile, observe_acu):
    """Enable the VCU pit-diag stream (0x7E0 'DEADBEEF') and expose
    read_fsm() -> the FSM state byte from 0x700[0]. NOTE pit_diag_bus is a
    PLACEHOLDER (can2); if 0x700 never appears it is on another FDCAN."""
    from tools.firmware_test.acu_stim import AcuStim
    bus = vcu_profile["pit_diag_bus"]
    _skip_if_no_can(bus)
    stim = AcuStim(channel=bus)
    stim.start()
    en_id = int(vcu_profile["pit_diag_enable_id"])
    stim.send_raw(en_id, bytes.fromhex("DEADBEEF"), is_extended_id=False)
    time.sleep(0.3)

    def read_fsm(timeout_s=1.5):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_PIT_STATUS, extended=False)
            if f is not None:
                return M.decode_pit_status(f.data)["fsm_state"]
            time.sleep(0.02)
        return None

    yield {"read_fsm": read_fsm}
    try:
        stim.send_raw(en_id, bytes([0] * 4), is_extended_id=False)
    finally:
        stim.stop()
