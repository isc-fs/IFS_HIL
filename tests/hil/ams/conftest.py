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
  - `current_heartbeat` optional DAC stim on PF11 so the current-stale
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

Tests requiring real hardware that the rig doesn't have (PF11 analog
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

# Register the KPI plugin so it sees all session events for AMS HIL
# runs. See `tests/hil/ams/kpi_plugin.py` and
# `docs/ams-hil/test-plan-v1.5.0.md` §4. Disable with `--no-kpi`.
pytest_plugins = ["tests.hil.ams.kpi_plugin"]


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
      - `set_volts(v)` — change the value on the fly (instant step; only
                        safe when AMS isn't actively gating on dV/dt)
      - `ramp_to(target_V, duration_ms=500, steps=10)` — linearly walk
                        from current value to target. Use this for
                        Precharge → Run / Charge transitions: real
                        precharge is an RC ramp through the 690 Ω
                        resistor, and AMS trips an Error from Precharge
                        if the bus jumps from 0 to ~pack in one tick.
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

    def ramp_to(target_V: int, duration_ms: int = 500, steps: int = 10) -> None:
        """Linearly walk volts from current → target over duration_ms.
        Default 10 × 50 ms aligns with the 50 ms heartbeat cadence, so the
        AMS sees one fresh frame per step (~34 V/step on the typical
        0 → 342 V ramp). No-op if already at target or duration ≤ 0."""
        start_V = int(state["volts"])
        target_V = int(target_V)
        if target_V == start_V or duration_ms <= 0 or steps <= 0:
            state["volts"] = target_V
            return
        step_s = (duration_ms / 1000.0) / steps
        for i in range(1, steps + 1):
            state["volts"] = int(start_V + (target_V - start_V) * i / steps)
            time.sleep(step_s)

    state["set_volts"] = set_volts
    state["pause"]     = pause
    state["resume"]    = resume
    state["ramp_to"]   = ramp_to

    yield state

    stop_evt.set()
    t.join(timeout=1.0)
    stim.stop()


@pytest.fixture
def current_heartbeat(ams_profile, mlc_powered):
    """Background thread that drives the MLC's PF11 (current-sensor Hall
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
    from a specific channel to a specific MLC's PF11 is bench-
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
# pack_current_diff -- differential pack-current injection (AMS #348 rework)
# ---------------------------------------------------------------------------

@pytest.fixture
def pack_current_diff(ams_profile, mlc_powered):
    """Drive the differential pack-current pair into the AMS (#348 current-
    sensor rework). DAC4 ch0 -> S_current_p (PF7/OUT_P), ch1 -> S_current_n
    (PF8/OUT_N); the firmware reads 5 mV/A of (OUT_P - OUT_N). A common-mode
    of ~1.44 V keeps both legs inside the 0-3.3 V rail across +-200 A.

        set_A(I): OUT_P = CM + I*(mV/A)/2 ; OUT_N = CM - I*(mV/A)/2   (volts)

    Opt-in: needs `pack_current_dac_idx` + `pack_current_dac_ch_p/_n` in
    ams_profile.yaml; a no-op (state dict only) otherwise -- the DAC -> PF7/PF8
    routing is bench-physical (DAC4 ch0/ch1; see CLAUDE.md DAC80504 wiring).

    Exposes (current_heartbeat-shaped):
      - `set_A(amps)`            -- set the injected pack current (default 0 A)
      - `pause()` / `resume()`   -- stop / restart driving (pause holds last V)
      - `A` / `paused`
    """
    period_s = float(ams_profile.get("pack_current_period_ms", 50)) / 1000.0
    dac_idx  = ams_profile.get("pack_current_dac_idx")
    ch_p     = ams_profile.get("pack_current_dac_ch_p")
    ch_n     = ams_profile.get("pack_current_dac_ch_n")
    cm_v     = float(ams_profile.get("pack_current_cm_v", 1.44))
    mv_per_a = float(ams_profile.get("pack_current_mv_per_a", 5.0))
    enabled  = dac_idx is not None and ch_p is not None and ch_n is not None

    state = {
        "A":         float(ams_profile.get("pack_current_default_a", 0)),
        "paused":    False,
        "enabled":   enabled,
        "dac_idx":   dac_idx,
        "ch_p":      ch_p,
        "ch_n":      ch_n,
        "cm_v":      cm_v,
        "mv_per_a":  mv_per_a,
        "period_ms": int(period_s * 1000),
    }

    def _legs(amps):
        half = amps * (mv_per_a / 1000.0) / 2.0   # half the differential, volts
        return cm_v + half, cm_v - half            # (OUT_P, OUT_N)

    if enabled:
        from broker.server import BrokerClient
        client = BrokerClient(
            os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))
        stop_evt = threading.Event()

        def _loop():
            while not stop_evt.is_set():
                if not state["paused"]:
                    try:
                        vp, vn = _legs(state["A"])
                        client.call("dac.set_voltage", idx=dac_idx,
                                    channel=ch_p, volts=vp)
                        client.call("dac.set_voltage", idx=dac_idx,
                                    channel=ch_n, volts=vn)
                    except Exception as e:
                        log.warning("pack_current_diff DAC write failed: %s", e)
                        break
                stop_evt.wait(period_s)

        t = threading.Thread(target=_loop, name="pack-current-diff", daemon=True)
        t.start()
        log.info("pack_current_diff: DAC%d ch%d/%d, CM %.2f V, %.1f mV/A "
                 "(start %.1f A)", dac_idx, ch_p, ch_n, cm_v, mv_per_a, state["A"])
    else:
        log.info("pack_current_diff: DISABLED (no pack_current_dac_idx + "
                 "pack_current_dac_ch_p/_n in ams_profile.yaml).")
        stop_evt = None
        t = None

    def set_A(amps): state["A"] = float(amps)
    def pause():     state["paused"] = True
    def resume():    state["paused"] = False

    state["set_A"]  = set_A
    state["pause"]  = pause
    state["resume"] = resume

    yield state

    if stop_evt is not None:
        stop_evt.set()
        if t is not None:
            t.join(timeout=1.0)
        try:   # park both legs at CM (0 A) so no current is left injected
            client.call("dac.set_voltage", idx=dac_idx, channel=ch_p, volts=cm_v)
            client.call("dac.set_voltage", idx=dac_idx, channel=ch_n, volts=cm_v)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# charger_0x101 -- operator charge-request emitter (AMS #311 / #316)
# ---------------------------------------------------------------------------

@pytest.fixture
def charger_0x101(ams_profile, mlc_powered):
    """Background emitter for the operator charge-request `0x101`.

    A connected charger broadcasts `0x101` with the magic 'CHRG'
    (0x43485247) at >= 2 Hz. The AMS uses this charge-request freshness
    to (a) select Charger mode at the mode lock when the VCU `0x100` is
    absent and (b) gate the Charger Precharge->Transition proceed
    (one-button, no second press -- #316). Request it for Charger-path
    tests; Car-path tests simply don't (no `0x101` -> Car).

    Exposes (acu_heartbeat-shaped):
      - `pause()` / `resume()` -- stop / restart emission (pause simulates
                                  a charger disconnect, C-037c)
      - `paused`               -- current state
    """
    from tools.firmware_test.acu_stim import AcuStim

    bus = ams_profile["bus_acu"]
    _skip_if_no_can(bus)

    period_s = float(ams_profile.get("charger_0x101_period_ms", 200)) / 1000.0
    can_id   = int(ams_profile.get("charger_0x101_id", 0x101))
    payload  = bytes.fromhex(str(ams_profile.get("charger_0x101_payload",
                                                 "4348524700000000")))

    stim = AcuStim(channel=bus)
    stim.start()
    state = {"paused": False, "id": can_id, "period_ms": int(period_s * 1000)}
    stop_evt = threading.Event()

    def _loop():
        while not stop_evt.is_set():
            if not state["paused"]:
                try:
                    stim.send_raw(can_id, payload, is_extended_id=False)
                except Exception as e:
                    log.warning("charger_0x101 send failed: %s", e)
                    break
            stop_evt.wait(period_s)

    t = threading.Thread(target=_loop, name="charger-0x101", daemon=True)
    t.start()
    log.info("charger_0x101 emitting 0x%X %s @ %d ms",
             can_id, payload.hex(), int(period_s * 1000))

    state["pause"]  = lambda: state.__setitem__("paused", True)
    state["resume"] = lambda: state.__setitem__("paused", False)

    yield state

    stop_evt.set()
    t.join(timeout=1.0)
    stim.stop()


# ---------------------------------------------------------------------------
# balance_override -- operator balance-control 0x103 sender (AMS #340)
# ---------------------------------------------------------------------------

@pytest.fixture
def balance_override(ams_profile, mlc_powered):
    """Background `0x103` sender for the operator balance-override (#340).

    The operator/charger broadcasts `0x103` at >= 2 Hz with a magic payload:
    'BALO' suppresses autonomous balancing, 'BALX' resumes auto. If the frame
    goes stale (> BalanceOverrideFreshMs = 5 s) the AMS reverts to auto.
    Charge-only.

    Exposes:
      - `send(magic)` -- start emitting at ~5 Hz; `magic` is 'BALO' / 'BALX' /
                         'BALZ' (a wrong-magic probe) or raw 4 bytes.
      - `stop()`      -- stop emitting (let the override go stale).
    """
    from tools.firmware_test.acu_stim import AcuStim
    from tools.firmware_test.ams import can_map as M

    bus = ams_profile["bus_acu"]
    _skip_if_no_can(bus)

    stim = AcuStim(channel=bus)
    stim.start()
    state = {"payload": None}
    stop_evt = threading.Event()
    period_s = 0.2   # 5 Hz, well above the >= 2 Hz freshness floor

    def _loop():
        while not stop_evt.is_set():
            p = state["payload"]
            if p is not None:
                try:
                    stim.send_raw(M.ID_BALANCE_OVERRIDE, p, is_extended_id=False)
                except Exception as e:
                    log.warning("balance_override send failed: %s", e)
                    break
            stop_evt.wait(period_s)

    t = threading.Thread(target=_loop, name="balance-0x103", daemon=True)
    t.start()

    _MAGIC = {
        "BALO": M.BALANCE_OVERRIDE_SUPPRESS,
        "BALX": M.BALANCE_OVERRIDE_RESUME,
        "BALZ": bytes([0x42, 0x41, 0x4C, 0x5A]),   # wrong magic (B-05)
    }

    def _send(magic):
        state["payload"] = _MAGIC[magic] if isinstance(magic, str) else magic

    def _stop():
        state["payload"] = None

    yield {"send": _send, "stop": _stop}

    stop_evt.set()
    t.join(timeout=1.0)
    stim.stop()


# ---------------------------------------------------------------------------
# wait_for_settled -- telemetry settle after fresh_boot (AMS #301 / first poll)
# ---------------------------------------------------------------------------

@pytest.fixture
def wait_for_settled(observe_acu, ams_profile):
    """Wait for telemetry to settle after `fresh_boot`.

    `fresh_boot` returns at the *first* 0x4A0, which carries partial-
    first-poll cells (some modules not yet read) and AMS_OK not yet
    asserted (it waits for grace + first full poll, ~4.4 s -- AMS #301).
    Poll for a 0x4A0 where min_cell == the stub seed AND AMS_OK is high,
    so tests read a steady-state frame instead of racing the boot.
    """
    from tools.firmware_test.ams import can_map as M

    def _wait(timeout_s: float = 8.0) -> dict:
        stub_mv = int(ams_profile["stub_cell_mV"])
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                last = M.decode_telem_status(f.data)
                if last["min_cell_mV"] == stub_mv and last["ams_ok"]:
                    return last
            time.sleep(0.05)
        raise AssertionError(
            f"telemetry did not settle (min_cell=={stub_mv} & AMS_OK) "
            f"within {timeout_s}s. Last: {last}")
    return _wait


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
# Pit-diag stream enable (AMS PR #248 + #263 + #269)
# ---------------------------------------------------------------------------

@pytest.fixture
def pit_diag(ams_profile, mlc_powered, observe_acu):
    """Enable the AMS pit-diag burst (1 Hz, 58 frames on 0x680..0x6C8)
    for the duration of the test. Disables on teardown.

    Yields a small helper with:
      - `wait_for(can_id, timeout_s)` → bytes payload of next 0x6Cx frame
      - `wait_for_scan(timeout_s)`    → blocks until at least one complete
                                        scan cycle has been observed
        (defined as: 0x6C6 firmware-ID arriving after enable, since it's
        the last frame in tx_pit_diag_scan before the per-IC PEC pair).

    Requires AMS firmware post-#248 (pit-diag stream landed) and
    post-#263 (TX-FIFO flow control) so all 58 frames reach the wire.
    """
    import subprocess
    from tools.firmware_test.ams import can_map as M

    bus = ams_profile["bus_acu"]
    _skip_if_no_can(bus)

    def _send_cmd(payload: bytes) -> None:
        # cansend ID#HEXBYTES — assume bash on the runner.
        hex_payload = payload.hex().upper()
        r = subprocess.run(
            ["cansend", bus, f"{M.ID_PIT_DIAG_CMD:03X}#{hex_payload}"],
            capture_output=True, text=True, timeout=2.0,
        )
        if r.returncode != 0:
            pytest.skip(f"cansend rejected pit-diag cmd on {bus}: "
                        f"{r.stderr.strip()}")

    class _PitDiag:
        def wait_for(self, can_id: int, timeout_s: float = 2.5) -> bytes:
            """Block up to `timeout_s` for the next frame at `can_id`
            (default 2.5 s = 2 scan periods + slack). Returns the bytes
            payload."""
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                f = observe_acu.last(can_id, extended=False)
                if f is not None:
                    return bytes(f.data[: f.dlc])
                time.sleep(0.02)
            raise AssertionError(
                f"no pit-diag frame 0x{can_id:03X} observed within "
                f"{timeout_s:.1f} s -- is the AMS firmware post-#248 "
                "(pit-diag stream landed)? Is the bus the right one "
                f"(observing {bus})?")

        def wait_for_scan(self, timeout_s: float = 2.5) -> None:
            """Block until at least one complete scan has been observed.
            We anchor on 0x6C6 (firmware-ID) since it's the last frame
            in tx_pit_diag_scan before the per-IC PEC pair."""
            observe_acu.clear()
            self.wait_for(M.ID_PIT_DIAG_FW_ID, timeout_s)

    # Enable
    _send_cmd(M.PIT_DIAG_ENABLE_MAGIC)
    # Wait for ACK so the test doesn't race the enable.
    helper = _PitDiag()
    try:
        helper.wait_for(M.ID_PIT_DIAG_ACK, timeout_s=0.5)
    except AssertionError:
        pytest.skip("pit-diag enable ACK (0x7F1) not seen within 0.5 s "
                    "-- AMS firmware may be pre-#248. Confirm with "
                    "`gh issue view 248 -R isc-fs/IFS08-CE-AMS`.")

    try:
        yield helper
    finally:
        # Best-effort disable on teardown so the next test starts
        # without a stale enable.
        try:
            _send_cmd(M.PIT_DIAG_DISABLE_MAGIC)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Relay readback (AMS GPIO outputs sampled via bench ADC3 / MCP3208)
# ---------------------------------------------------------------------------

@pytest.fixture
def relays_readback(ams_profile, mlc_powered):
    """Read AMS_OK / AIR+ / AIR- / PRECH GPIO state via MCP3208.
    Opt-in: requires the 8 *_adc_idx / *_adc_channel keys + the
    threshold to be present in ams_profile.yaml. Yields None and
    tests using it skip if any key is absent."""
    names = ("ams_ok", "air_p", "air_n", "prech")
    missing = [k for n in names
               for k in (f"{n}_adc_idx", f"{n}_adc_channel")
               if k not in ams_profile]
    if missing:
        log.info("relays_readback: DISABLED (missing keys: %s)",
                 ", ".join(missing))
        yield None
        return

    channels = {
        n: (int(ams_profile[f"{n}_adc_idx"]),
            int(ams_profile[f"{n}_adc_channel"]))
        for n in names
    }
    threshold_v = float(
        ams_profile.get("relay_readback_digital_threshold_v", 1.65))

    from broker.server import BrokerClient
    client = BrokerClient(
        os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))

    class RelaysReadback:
        def _read_volts(self, name: str) -> float:
            idx, ch = channels[name]
            return float(client.call("adc.read_voltage", idx=idx, channel=ch))

        def read(self) -> dict:
            """Single-shot snapshot of all 4 lines as booleans (True = HIGH)."""
            return {n: self._read_volts(n) > threshold_v for n in names}

        def read_volts(self) -> dict:
            """Snapshot of all 4 lines as raw voltages (for diagnostics)."""
            return {n: self._read_volts(n) for n in names}

        def poll_for(self, predicate, timeout_s: float,
                     period_s: float = 0.005) -> dict | None:
            """Poll read() until predicate(state) truthy; return matching
            state on success, None on timeout."""
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                s = self.read()
                if predicate(s):
                    return s
                time.sleep(period_s)
            return None

        def sample_for(self, duration_s: float,
                       period_s: float = 0.005) -> list:
            """Return list of (t_offset, state) samples for duration_s."""
            t0 = time.monotonic()
            deadline = t0 + duration_s
            samples = []
            while time.monotonic() < deadline:
                samples.append((time.monotonic() - t0, self.read()))
                time.sleep(period_s)
            return samples

    log.info("relays_readback ready: ams_ok=%s air_p=%s air_n=%s prech=%s "
             "(threshold=%.2f V)",
             channels["ams_ok"], channels["air_p"], channels["air_n"],
             channels["prech"], threshold_v)
    try:
        yield RelaysReadback()
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Pico LTC emulator client (v0.3.0+ for STOP_REPLY / inject_*)
# ---------------------------------------------------------------------------

@pytest.fixture
def pico_emu():
    """USB-CDC client for the Pi Pico LTC6811 emulator on MLC2 J8.

    Skips cleanly when the Pico isn't reachable -- lets the test
    suite stay green off-bench and on rigs where the emulator isn't
    wired. Per-test scope so each test starts from a clean reset
    state (`reset_state` on teardown also drops any STOP_REPLY mask
    left over by a previous test).

    Used by E-065 / E-066 / E-067 (LTC chain integrity) and B-026
    (cell UV/OV/OT/UT fault injection).
    """
    try:
        from tools.pico_ltc_emulator.host.pico_ltc_client import PicoLtcClient
    except Exception as e:
        pytest.skip(f"Pico LTC client import failed: {e}")
    try:
        client = PicoLtcClient()
        client.open()
        # Probe — if PING fails, the Pico either isn't there or is in
        # BOOTSEL. Skip rather than fail.
        try:
            client.ping()
        except Exception as e:
            client.close()
            pytest.skip(f"Pico LTC emulator did not respond to PING: {e}")
    except Exception as e:
        pytest.skip(f"Pico LTC emulator unreachable: {e}")
    try:
        yield client
    finally:
        # Best-effort cleanup so the next test sees nominal-healthy seed.
        # NB: reset_state() only drops the STOP_REPLY mask -- it does NOT
        # clear cell/temp injections. Without the set_all_* below, B-026's
        # inject_cell_* persists in the Pico and poisons later tests,
        # including ones that don't request pico_emu (fresh_boot power-
        # cycles the AMS chip, not the Pico). Force every cell and temp
        # back to the nominal-healthy seed.
        try: client.resume_all()
        except Exception: pass
        try: client.reset_state()
        except Exception: pass
        try: client.set_all_cells(3750)   # nominal cell, A-005
        except Exception: pass
        try: client.set_all_temps(250)    # 25.0 C (deci-degC), A-007
        except Exception: pass
        client.close()


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

def _robust_power_on(client, relay_bit, ina_addr, min_mA, observe_acu=None,
                     *, settle_s=0.3, retries=4):
    """Close the carrier relay and VERIFY the chip actually drew power.

    The TCA9555/relay contact is intermittent on this bench: a bare
    `write_pin(True)` can leave the carrier unpowered (INA ~0) or only
    briefly powered (telemeters at boot, then drops). Re-toggle until INA
    reads a solid >= min_mA across two samples, clearing the observer (if
    given) before the winning close so the caller's telemetry wait sees
    post-power-on frames. Returns the monotonic power-on time; fails loudly
    if the carrier never comes up -- that's a bench/relay fault, not firmware.
    """
    mA = 0.0
    for attempt in range(1, retries + 1):
        if observe_acu is not None:
            observe_acu.clear()
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
                log.warning("carrier powered only on relay attempt %d (%.0f mA)",
                            attempt, mA)
            return t_on
        client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
        time.sleep(0.4)
    pytest.fail(
        f"carrier never drew >= {min_mA:.0f} mA after {retries} relay closes "
        f"(last {mA:.0f} mA, INA 0x{ina_addr:02x}) -- intermittent TCA/relay or "
        "carrier seating, not a firmware fault."
    )


@pytest.fixture
def fresh_boot(ams_profile, mlc_powered, observe_acu, acu_heartbeat,
               pack_current_diff):
    """Power-cycle the MLC, let the BL auto-jump to the app, return once
    the first `0x4A0` telemetry frame has arrived.

    Returns a dict:
      - `first_frame`: the decoded first 0x4A0 (state, ams_ok, mask, …)
      - `t_power_on`: monotonic time when K_n closed
      - `t_first_frame`: monotonic time of the first 0x4A0

    `acu_heartbeat` is started before power-on so the VCU staleness
    predicate sees fresh data within the boot-grace window.

    `pack_current_diff` (not the legacy single-ended `current_heartbeat`)
    drives the PF7/PF8 pair to CM = 0 A. The #348/#350 firmware reads PF7-PF8
    differentially, so a single-ended PF7=2.5 V drive reads as a massive
    over-current and latches Error before a test can reach a clean baseline
    (M-043 surfaced this across Block B). This is the #348 harness migration.
    """
    from broker.server import BrokerClient
    from tools.firmware_test.ams import can_map as M

    client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                         "/run/hil-broker/broker.sock"))
    relay_bit = mlc_powered["relay_bit"]
    try:
        client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
        # Drop TSMS + DASH_CHG BEFORE power-on. TCA9555 outputs survive
        # an MLC power-cycle (the TCA itself stays powered), and pytest
        # creates this fixture before tsms/dash_chg, so without an
        # explicit deassert here the chip can boot with leftover-HIGH
        # cockpit inputs and jump straight Start -> Precharge. Tests
        # that check `first_frame['state'] == Start` then fail.
        # Idempotent: only writes the keys actually present in
        # ams_profile, so it's a no-op for benches that don't wire TSMS
        # or DASH_CHG through a TCA.
        for key_prefix in ("tsms", "dash_chg"):
            addr_key = f"{key_prefix}_tca_addr"
            port_key = f"{key_prefix}_tca_port"
            pin_key  = f"{key_prefix}_tca_pin"
            if all(k in ams_profile for k in (addr_key, port_key, pin_key)):
                addr = int(ams_profile[addr_key])
                port = int(ams_profile[port_key])
                pin  = int(ams_profile[pin_key])
                # Set direction = output for both cockpit pins on this
                # (addr, port) so the write below actually drives the
                # line. _combined_output_mask handles the case where
                # both pins share a port.
                mask = _combined_output_mask(ams_profile, addr, port)
                client.call("tca.set_direction", addr=addr, port=port, mask=mask)
                client.call("tca.write_pin", addr=addr, port=port,
                            pin=pin, value=False)
        time.sleep(2.0)
        t_power_on = _robust_power_on(
            client, relay_bit, mlc_powered["ina_addr"],
            float(ams_profile["mlc_boot_current_mA"]), observe_acu)
        # KPI: this fixture is the canonical per-test power-cycle.
        # Counted toward `power_cycle_count` for relay K_n + carrier
        # connector reseat-wear accounting.
        from tests.hil.ams import kpi_plugin
        kpi_plugin.bump_power_cycle()
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


def _boot_diff_core(ams_profile, mlc_powered, observe_acu):
    """Relay power-cycle to a clean Start and return the first decoded 0x4A0.

    Mirror of `fresh_boot`'s core, kept standalone so the #348 current-sensor
    block can boot with the DIFFERENTIAL current source active. `fresh_boot`
    proper still pulls in the single-ended `current_heartbeat`, which drives
    DAC4 ch0 (PF7) only and fights the differential `pack_current_diff` on the
    same channel — wrong input for the PF7/PF8 rework firmware. The #348
    migration will fold `fresh_boot` onto `pack_current_diff` and drop this.
    """
    from broker.server import BrokerClient
    from tools.firmware_test.ams import can_map as M

    client = BrokerClient(
        os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))
    relay_bit = mlc_powered["relay_bit"]
    try:
        client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
        # Drop TSMS + DASH_CHG before power-on (TCA outputs survive an MLC
        # power-cycle) so the chip boots to Start, not Start->Precharge.
        for key_prefix in ("tsms", "dash_chg"):
            ak, pk, nk = (f"{key_prefix}_tca_addr", f"{key_prefix}_tca_port",
                          f"{key_prefix}_tca_pin")
            if all(k in ams_profile for k in (ak, pk, nk)):
                addr, port, pin = (int(ams_profile[ak]), int(ams_profile[pk]),
                                   int(ams_profile[nk]))
                mask = _combined_output_mask(ams_profile, addr, port)
                client.call("tca.set_direction", addr=addr, port=port, mask=mask)
                client.call("tca.write_pin", addr=addr, port=port, pin=pin, value=False)
        time.sleep(2.0)
        t_power_on = _robust_power_on(
            client, relay_bit, mlc_powered["ina_addr"],
            float(ams_profile["mlc_boot_current_mA"]), observe_acu)
        from tests.hil.ams import kpi_plugin
        kpi_plugin.bump_power_cycle()
    finally:
        client.close()

    # The current-sensor-rework firmware boots slower (CubeMX-regenerated,
    # RTOS/HAL bump) than the single-ended build, so allow more slack than
    # fresh_boot's 5 s before declaring the app dead.
    deadline = time.monotonic() + 12.0
    first = None
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
        if f is not None:
            first = f
            break
        time.sleep(0.05)
    if first is None:
        pytest.fail(
            "No 0x4A0 telemetry within 12 s of power-on. current-sensor-diff "
            "app didn't reach MainTask, or FDCAN1 isn't transmitting.")

    return {
        "first_frame":   M.decode_telem_status(first.data),
        "t_power_on":    t_power_on,
        "t_first_frame": time.monotonic(),
    }


@pytest.fixture
def fresh_boot_diff(ams_profile, mlc_powered, observe_acu, acu_heartbeat,
                    pack_current_diff):
    """`fresh_boot` variant for the #348 current-sensor firmware: drives the
    differential pack-current pair (`pack_current_diff`) instead of the
    single-ended `current_heartbeat`. Same contract (returns first 0x4A0 +
    timing). `acu_heartbeat` + `pack_current_diff` are active before power-on
    so VCU + current freshness predicates see data inside the boot grace."""
    return _boot_diff_core(ams_profile, mlc_powered, observe_acu)


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
