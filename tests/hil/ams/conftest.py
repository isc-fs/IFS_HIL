"""
AMS HIL test fixtures (v1.3.0-flatten / chain-less rig).

Aligned with `isc-fs/IFS08-CE-AMS#123`. The firmware is built with
`-DAMS_BMS_HIL_STUB=1` so `BmsPollTask::seed_for_hil_stub` populates a
nominal-healthy `BmsState` internally — there's no bench-side BMS
emulator on FDCAN2 anymore.

Fixtures provided here, layered on top of `tests/hil/conftest.py`'s
`broker_available` + `psu_on`:

  - `ams_profile`     YAML thresholds & cadences (session-scoped)
  - `mlc_powered`     energises the carrier slot, waits for boot
  - `flasher`         `CanFlasher` pre-pointed at FDCAN2 (BL bus)
  - `observe_acu`     passive sniffer on FDCAN1 (telemetry)
  - `acu_heartbeat`   periodic `0x100` so the VCU staleness predicate
                      doesn't trip after grace
  - `current_heartbeat` optional DAC stim on PF7 so the current-stale
                      predicate doesn't trip in non-stub flight builds
                      (no-op when `current_heartbeat_dac_idx` /
                      `current_heartbeat_dac_channel` are absent from
                      ams_profile.yaml; relies on AMS PR #182's
                      HIL_STUB gate in that case)
  - `acu_stim`        one-shot ACU stim helper (DC bus voltage one-off);
                      start_button / charger frames retired upstream in
                      isc-fs/IFS08-CE-AMS#187
  - `tsms`            TSMS pin driver (side-of-car external master
                      switch, PF9). Opt-in: requires `tsms_tca_addr`,
                      `tsms_tca_port`, `tsms_tca_pin` in
                      ams_profile.yaml. Yields None when any key is
                      absent and tests that need it skip.
  - `dash_chg`        DASH_CHG pin driver (cockpit dashboard / charger
                      button, PF10). Opt-in: requires
                      `dash_chg_tca_addr/_port/_pin`. Same skip
                      behaviour.
  - `fresh_boot`      power-cycle MLC, wait for app to come up; returns
                      the first decoded `0x4A0` (state byte, etc.)
  - `wait_for_state`  poll `0x4A0` until state == expected
  - `wait_for_heartbeat_advance` poll `0x4A2[7]` until counter advances

Tests requiring real hardware that the rig doesn't have (PF7 analog
stim, GDB attach, scope) call `pytest.skip` with a clear reason — the
suite stays green off-bench.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

import pytest
import yaml

from tools.hil_client import psu_status                       # noqa: F401

log = logging.getLogger(__name__)

PROFILE_PATH = Path(__file__).parent / "ams_profile.yaml"


# ---------------------------------------------------------------------------
# CLI options
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--soak-scale", action="store", default="1.0", type=float,
        help=(
            "Scale factor for Block E soak durations "
            "(default 1.0 = full, 0.1 makes a 30-minute soak run for 3 min)."
        ),
    )


@pytest.fixture(scope="session")
def soak_scale(request) -> float:
    return float(request.config.getoption("--soak-scale"))


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
    """Energise the AMS carrier slot, wait for the BL to settle, hand back
    to the test. De-energises on session teardown."""
    from broker.server import BrokerClient
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
    yield {"slot": slot, "relay_bit": relay_bit, "ina_addr": ina_addr,
           "current_mA": current_mA}

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
def observe_acu(ams_profile, mlc_powered):
    """Passive CAN sniffer on the ACU bus (FDCAN1, kernel `can0`)."""
    from tools.firmware_test.can_observer import CanObserver
    bus = ams_profile["bus_acu"]
    _skip_if_no_can(bus)
    with CanObserver(channel=bus) as obs:
        yield obs


@pytest.fixture
def acu_stim(ams_profile, mlc_powered):
    """One-shot ACU stimulus (start button, charger, DC bus one-off).

    Coexists with `acu_heartbeat` — separate `AcuStim` instance, separate
    SocketCAN socket — so a test can mix periodic heartbeat with sporadic
    transition stimuli."""
    from tools.firmware_test.acu_stim import AcuStim
    bus = ams_profile["bus_acu"]
    _skip_if_no_can(bus)
    with AcuStim(channel=bus) as a:
        yield a


@pytest.fixture
def acu_heartbeat(ams_profile, mlc_powered):
    """Background thread that emits `0x100` DC-bus frames at 20 Hz so the
    VCU staleness predicate doesn't trip after boot grace.

    Exposes:
      - `set_volts(v)` — change the value on the fly (C-022 ramp-to-target)
      - `pause()`      — stop emission (B-017 / C-* fault injection)
      - `resume()`     — restart emission after a pause
      - `volts`        — current value
      - `paused`       — current state
    """
    from tools.firmware_test.acu_stim import AcuStim

    bus = ams_profile["bus_acu"]
    _skip_if_no_can(bus)

    period_s = float(ams_profile["acu_heartbeat_period_ms"]) / 1000.0
    state = {
        "volts":     int(ams_profile["acu_heartbeat_dc_bus_v"]),
        "paused":    False,
        "channel":   bus,
        "period_ms": int(period_s * 1000),
    }

    stim = AcuStim(channel=bus)
    stim.start()
    stop_evt = threading.Event()

    def _loop():
        while not stop_evt.is_set():
            if not state["paused"]:
                try:
                    stim.send_dc_bus_v(state["volts"])
                except Exception as e:
                    log.warning("acu_heartbeat send failed: %s", e)
                    break
            stop_evt.wait(period_s)

    t = threading.Thread(target=_loop, name="acu-heartbeat", daemon=True)
    t.start()
    log.info("ACU heartbeat running on %s @ %d ms (start volts=%d)",
             bus, int(period_s * 1000), state["volts"])

    def set_volts(v: int) -> None: state["volts"] = int(v)
    def pause() -> None:           state["paused"] = True
    def resume() -> None:          state["paused"] = False

    state["set_volts"] = set_volts
    state["pause"]     = pause
    state["resume"]    = resume

    yield state

    stop_evt.set()
    t.join(timeout=1.0)
    stim.stop()


@pytest.fixture
def current_heartbeat(ams_profile, mlc_powered):
    """Background thread that drives the MLC's PF7 (current-sensor Hall
    input) via a backplane DAC channel so the firmware's current
    freshness + Imax predicates see a plausible "0 A" reading.

    Layered on top of AMS PR #182 (which gated the current-stale check
    under -DAMS_BMS_HIL_STUB): when the firmware is built without the
    stub, this fixture restores meaningful current-predicate coverage
    on the bench. When the firmware IS stub-gated, it's still useful
    because:
      - `sensor_fault` and `|filtered_mA| > kImaxMa` checks remain
        active even under HIL_STUB
      - tests that want to inject a non-zero current (e.g. future
        current-overlimit fault tests) can call `set_mA(value)`

    Opt-in: requires `current_heartbeat_dac_idx` and
    `current_heartbeat_dac_channel` in ams_profile.yaml. If either is
    absent, the fixture is a no-op (background thread doesn't drive
    the DAC) — tests still get a state dict so they can call
    `set_mA / pause / resume` without conditionals. The DAC routing
    from a specific channel to a specific MLC's PF7 is bench-
    physical (J2 pinout dependent) and must be verified before
    enabling; see CLAUDE.md for the DAC80504 wiring.

    Exposes (same shape as `acu_heartbeat`):
      - `set_mA(mA)`  — set the simulated current (default 0)
      - `pause()`     — stop driving (DAC holds last value)
      - `resume()`    — resume driving
      - `mA`          — current value
      - `paused`      — current state
    """
    period_s = float(ams_profile.get("current_heartbeat_period_ms", 50)) / 1000.0
    dac_idx     = ams_profile.get("current_heartbeat_dac_idx")
    dac_channel = ams_profile.get("current_heartbeat_dac_channel")
    enabled     = dac_idx is not None and dac_channel is not None

    state = {
        "mA":        int(ams_profile.get("current_heartbeat_default_mA", 0)),
        "paused":    False,
        "enabled":   enabled,
        "dac_idx":   dac_idx,
        "dac_channel": dac_channel,
        "period_ms": int(period_s * 1000),
    }

    def _ma_to_volts(mA: int) -> float:
        # Hall-effect transducer model from ams_config.hpp:
        #   2.5 V at 0 A, 5.7 mV/A sensitivity
        # (kCurrentZeroMv=2500, kCurrentMvPerAmpe1=57 / 10)
        return 2.5 + (mA / 1000.0) * (0.057 / 10.0)  # mA → A → V

    if enabled:
        from broker.server import BrokerClient
        client = BrokerClient(
            os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))
        stop_evt = threading.Event()

        def _loop():
            while not stop_evt.is_set():
                if not state["paused"]:
                    try:
                        client.call("dac.set_voltage",
                                    idx=dac_idx,
                                    channel=dac_channel,
                                    volts=_ma_to_volts(state["mA"]))
                    except Exception as e:
                        log.warning("current_heartbeat DAC write failed: %s", e)
                        break
                stop_evt.wait(period_s)

        t = threading.Thread(target=_loop, name="current-heartbeat", daemon=True)
        t.start()
        log.info("current_heartbeat driving DAC %d ch %d @ %d ms (start mA=%d)",
                 dac_idx, dac_channel, int(period_s * 1000), state["mA"])
    else:
        log.info("current_heartbeat: DISABLED (no current_heartbeat_dac_idx + "
                 "current_heartbeat_dac_channel in ams_profile.yaml). "
                 "Relying on AMS PR #182's stub-gated current_stale check.")
        stop_evt = None
        t = None

    def set_mA(mA: int) -> None: state["mA"] = int(mA)
    def pause() -> None:         state["paused"] = True
    def resume() -> None:        state["paused"] = False

    state["set_mA"]  = set_mA
    state["pause"]   = pause
    state["resume"]  = resume

    yield state

    if stop_evt is not None:
        stop_evt.set()
        if t is not None:
            t.join(timeout=1.0)


# ---------------------------------------------------------------------------
# TSMS + DASH_CHG GPIO stimulus -- two independent fixtures, one per pin.
#
# TSMS lives on the SIDE of the car (external operator master switch);
# DASH_CHG is the cockpit dashboard / charger button. Both replaced the
# retired 0x600 / 0x18FF50E7 CAN triggers in isc-fs/IFS08-CE-AMS#187.
# ---------------------------------------------------------------------------

def _combined_output_mask(ams_profile: dict, addr: int, port: int) -> int:
    """Mask for `tca.set_direction` covering BOTH TSMS and DASH_CHG when
    they share an (addr, port). Bit=0 -> output, bit=1 -> input. We can't
    write a single-bit direction change through `tca.set_direction`
    (it's a full-port write), so each pin fixture writes the same
    combined mask -- idempotent and race-free regardless of which
    fixture runs first."""
    out_pins: set[int] = set()
    if (int(ams_profile.get("tsms_tca_addr", -1)) == addr and
        int(ams_profile.get("tsms_tca_port", -1)) == port):
        out_pins.add(int(ams_profile["tsms_tca_pin"]))
    if (int(ams_profile.get("dash_chg_tca_addr", -1)) == addr and
        int(ams_profile.get("dash_chg_tca_port", -1)) == port):
        out_pins.add(int(ams_profile["dash_chg_tca_pin"]))
    mask = 0xFF
    for p in out_pins:
        mask &= ~(1 << p) & 0xFF
    return mask


def _tca_pin_fixture(ams_profile, key_prefix: str, label: str):
    """Shared body for the tsms / dash_chg fixtures. Opt-in via three
    keys; yields a `TcaPin` (or None if any key absent)."""
    keys = (f"{key_prefix}_tca_addr", f"{key_prefix}_tca_port",
            f"{key_prefix}_tca_pin")
    missing = [k for k in keys if k not in ams_profile]
    if missing:
        log.info("%s: DISABLED (missing ams_profile keys: %s). "
                 "Tests needing it will skip. Fill in once the TCA9555 "
                 "channel is wired to the AMS pin.",
                 label, ", ".join(missing))
        yield None
        return

    from broker.server import BrokerClient
    from tools.firmware_test.tca_pin import TcaPin

    addr = int(ams_profile[f"{key_prefix}_tca_addr"])
    port = int(ams_profile[f"{key_prefix}_tca_port"])
    pin  = int(ams_profile[f"{key_prefix}_tca_pin"])

    client = BrokerClient(
        os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))
    try:
        # Configure direction for THIS port covering both TSMS and
        # DASH_CHG (when they share a port). Idempotent.
        mask = _combined_output_mask(ams_profile, addr, port)
        client.call("tca.set_direction", addr=addr, port=port, mask=mask)
        helper = TcaPin(client, addr, port, pin, label=label)
        helper.deassert()
        log.info("%s ready: tca0x%02x p%d.b%d", label, addr, port, pin)
        yield helper
        helper.deassert()
    finally:
        client.close()


@pytest.fixture
def tsms(ams_profile, mlc_powered):
    """TSMS (Tractive System Master Switch, PF9) pin driver -- the
    side-of-car external master switch operated by a non-driver per
    FS rules. Driven HIGH = TS enabled."""
    yield from _tca_pin_fixture(ams_profile, "tsms", "TSMS")


@pytest.fixture
def dash_chg(ams_profile, mlc_powered):
    """DASH_CHG (PF10) pin driver -- the cockpit dashboard / charger
    button operated by the driver. Driven HIGH = request."""
    yield from _tca_pin_fixture(ams_profile, "dash_chg", "DASH_CHG")


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
# Power-cycle + first-telemetry helper (used by every block)
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_boot(ams_profile, mlc_powered, observe_acu, acu_heartbeat,
               current_heartbeat):
    """Power-cycle the MLC, let the BL auto-jump to the app, return once
    the first `0x4A0` telemetry frame has arrived.

    Returns a dict:
      - `first_frame`: the decoded first 0x4A0 (state, ams_ok, mask, …)
      - `t_power_on`: monotonic time when K_n closed
      - `t_first_frame`: monotonic time of the first 0x4A0

    `acu_heartbeat` is started before power-on so the VCU staleness
    predicate sees fresh data within the boot-grace window.
    """
    from broker.server import BrokerClient
    from tools.firmware_test.ams import can_map as M

    client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                         "/run/hil-broker/broker.sock"))
    relay_bit = mlc_powered["relay_bit"]
    try:
        client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
        time.sleep(2.0)

        # Pre-drive TSMS / DASH_CHG LOW BEFORE re-powering, so the chip
        # never sees the lines floating high during boot. Bench MLC
        # carriers don't have the AMS PCB's external pull-downs, so
        # without this the TCA9555's weak input pull-up holds PF9/PF10
        # high, which trips the FSM out of Start within boot-grace.
        # Opt-in: only fires when tsms_tca_* / dash_chg_tca_* keys are
        # present in ams_profile.
        for prefix in ("tsms", "dash_chg"):
            addr_k = f"{prefix}_tca_addr"
            port_k = f"{prefix}_tca_port"
            pin_k  = f"{prefix}_tca_pin"
            if all(k in ams_profile for k in (addr_k, port_k, pin_k)):
                addr = int(ams_profile[addr_k])
                port = int(ams_profile[port_k])
                pin  = int(ams_profile[pin_k])
                mask = _combined_output_mask(ams_profile, addr, port)
                client.call("tca.set_direction", addr=addr, port=port, mask=mask)
                client.call("tca.write_pin", addr=addr, port=port,
                            pin=pin, value=False)

        observe_acu.clear()
        client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=True)
        t_power_on = time.monotonic()
    finally:
        client.close()

    # BL boot + auto-jump + first telemetry should land within ~3 s.
    # Allow 5 s slack for the first ever boot on a fresh-flashed chip.
    deadline = time.monotonic() + 5.0
    first = None
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
        if f is not None:
            first = f
            break
        time.sleep(0.05)

    if first is None:
        pytest.fail(
            "No 0x4A0 telemetry within 5 s of power-on. App didn't reach "
            "MainTask, or FDCAN1 isn't transmitting. INA / candump can0 "
            "would localise."
        )

    decoded = M.decode_telem_status(first.data)
    return {
        "first_frame":    decoded,
        "t_power_on":     t_power_on,
        "t_first_frame":  time.monotonic(),
    }


# ---------------------------------------------------------------------------
# Polling helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def wait_for_state(observe_acu, ams_profile):
    """Return a callable `wait(expected_state, timeout_ms=None)` that
    polls the latest `0x4A0` until byte 0 == expected_state. Returns the
    decoded payload on success; raises AssertionError on timeout."""
    from tools.firmware_test.ams import can_map as M

    def _wait(expected_state: int, timeout_ms: int | None = None,
              poll_interval_s: float = 0.02) -> dict:
        timeout_ms = timeout_ms or int(ams_profile["state_transition_window_ms"])
        deadline = time.monotonic() + timeout_ms / 1000.0
        last_decoded = None
        while time.monotonic() < deadline:
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
def heartbeat_helper(observe_acu, ams_profile):
    """Returns helpers to read and wait on the `0x4A2[7]` heartbeat
    counter. `read()` returns the latest counter or `None`. `wait_advance(n)`
    blocks until the counter has advanced by `n` modulo-256."""
    from tools.firmware_test.ams import can_map as M

    def _read() -> int | None:
        f = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        if f is None:
            return None
        return M.decode_telem_temps(f.data)["heartbeat"]

    def _wait_advance(baseline: int, n: int = 1,
                      timeout_s: float | None = None) -> int | None:
        period_ms = int(ams_profile["tx_telemetry_period_ms"])
        timeout_s = timeout_s or (n * period_ms / 1000.0 + 1.0)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            hb = _read()
            if hb is not None and (hb - baseline) % 256 >= n:
                return hb
            time.sleep(period_ms / 1000.0 / 4)
        return None

    return {"read": _read, "wait_advance": _wait_advance}
