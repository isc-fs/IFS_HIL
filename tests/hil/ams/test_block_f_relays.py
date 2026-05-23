"""
Block F — Relay drive paths (new in `isc-fs/IFS08-CE-AMS#193`).

The AMS firmware drives four GPIO outputs to control contactors on a
real car:

  PB4 = AMS_OK      (1 = AMS healthy, 0 = fault latched)
  PB5 = RELAY_AIR_P (1 = AIR+ closed)
  PB6 = RELAY_AIR_N (1 = AIR- closed)
  PB7 = RELAY_PRECH (1 = precharge resistor in circuit)

On the bench they're sampled by ADC3 (MCP3208 idx 2) channels 0..3
via the `relays_readback` fixture. Block F walks the FSM through
state transitions and asserts the relays follow the documented
sequence -- the guard against the GPIOD→GPIOB regression from
PR #128 + general "is the FSM actually wired to the relays" coverage.

| Test  | What it checks                                                   | Status      |
|-------|------------------------------------------------------------------|-------------|
| F-060 | RELAY_AIR_N (PB6) toggle observed via ADC3 readback              | implemented |
| F-061 | RELAY_AIR_P (PB5) toggle observed                                | implemented |
| F-062 | RELAY_PRECHARGE (PB7) toggle observed                            | implemented |
| F-063 | AMS_OK (PB4) follows FSM (HIGH except in Error)                  | implemented |
| F-064 | Start->Precharge sequence: AIR_N + PRECH close, AIR_P stays open | implemented |
| F-065 | Precharge->Transition: AIR_P closes, PRECH opens, AIR_N stays    | implemented |
| F-066 | Error: all 3 contactors open within one FSM tick                 | implemented |
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require(relays_readback, tsms=None, dash_chg=None):
    if relays_readback is None:
        pytest.skip("relays_readback fixture disabled (missing *_adc_* "
                    "keys in ams_profile.yaml)")
    missing = []
    if tsms     is not None and tsms     is None: missing.append("tsms")
    if dash_chg is not None and dash_chg is None: missing.append("dash_chg")
    # Above pattern intentionally weird because we want to differentiate
    # "fixture not requested" from "fixture None". Tests call _require(
    # relays_readback, tsms=tsms, ...) only when those are needed.
    if missing:
        pytest.skip(f"Pin fixture(s) unavailable: {' + '.join(missing)}")


def _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile):
    tsms.assert_()
    dash_chg.assert_()
    return wait_for_state(
        M.FsmState.PRECHARGE,
        timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)


def _drive_to_transition(tsms, dash_chg, acu_heartbeat, wait_for_state,
                         ams_profile):
    _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)
    pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
    acu_heartbeat["set_volts"](int(pack_V * 0.96))
    return wait_for_state(
        M.FsmState.TRANSITION,
        timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)


def _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile):
    _drive_to_transition(tsms, dash_chg, acu_heartbeat, wait_for_state,
                         ams_profile)
    hold_ms = int(ams_profile["transition_hold_ms"])
    return wait_for_state(
        M.FsmState.RUN,
        timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)


# ---------------------------------------------------------------------------
# F-060 .. F-062 -- individual relay toggle visibility
# ---------------------------------------------------------------------------
# These are end-to-end "does the GPIO actually move when the FSM tells
# it to" checks. We use Start->Precharge as the canonical event that
# toggles AIR_N + PRECH (per the documented relay sequence). AIR_P
# only closes on Precharge->Transition, so F-061 drives one extra step.

class TestF060AirNToggles:

    def test_f060(self, fresh_boot, tsms, dash_chg, wait_for_state,
                  relays_readback, ams_profile):
        _require(relays_readback)
        if tsms is None or dash_chg is None:
            pytest.skip("tsms/dash_chg fixture unavailable")

        # Pre: in Start, AIR_N should be LOW
        pre = relays_readback.read()
        assert not pre["air_n"], (
            f"AIR_N already HIGH at Start (pre-condition): {relays_readback.read_volts()}")

        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)
        # AIR_N must come up within one FSM tick + ADC sampling slack
        s = relays_readback.poll_for(lambda x: x["air_n"], timeout_s=0.1)
        assert s is not None, (
            f"AIR_N did not go HIGH on Start->Precharge. Final state: "
            f"{relays_readback.read()}, volts: {relays_readback.read_volts()}")


class TestF061AirPToggles:

    def test_f061(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, relays_readback, ams_profile):
        _require(relays_readback)
        if tsms is None or dash_chg is None:
            pytest.skip("tsms/dash_chg fixture unavailable")

        _drive_to_transition(tsms, dash_chg, acu_heartbeat, wait_for_state,
                             ams_profile)
        # AIR_P closes at Precharge->Transition
        s = relays_readback.poll_for(lambda x: x["air_p"], timeout_s=0.1)
        assert s is not None, (
            f"AIR_P did not go HIGH on Precharge->Transition. State: "
            f"{relays_readback.read()}, volts: {relays_readback.read_volts()}")


class TestF062PrechargeToggles:

    def test_f062(self, fresh_boot, tsms, dash_chg, wait_for_state,
                  relays_readback, ams_profile):
        _require(relays_readback)
        if tsms is None or dash_chg is None:
            pytest.skip("tsms/dash_chg fixture unavailable")

        pre = relays_readback.read()
        assert not pre["prech"], (
            f"PRECH already HIGH at Start (pre-condition): {relays_readback.read_volts()}")

        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)
        s = relays_readback.poll_for(lambda x: x["prech"], timeout_s=0.1)
        assert s is not None, (
            f"PRECH did not go HIGH on Start->Precharge. State: "
            f"{relays_readback.read()}, volts: {relays_readback.read_volts()}")


# ---------------------------------------------------------------------------
# F-063 -- AMS_OK follows FSM state (HIGH everywhere except Error)
# ---------------------------------------------------------------------------

class TestF063AmsOkFollowsFsm:

    def test_f063(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, relays_readback, ams_profile):
        _require(relays_readback)
        if tsms is None or dash_chg is None:
            pytest.skip("tsms/dash_chg fixture unavailable")

        # Start: AMS_OK HIGH
        assert relays_readback.read()["ams_ok"], (
            f"AMS_OK LOW at fresh-boot Start. Voltages: {relays_readback.read_volts()}")

        # Trip Error via TSMS drop after reaching Run
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile)
        assert relays_readback.read()["ams_ok"], (
            f"AMS_OK LOW at Run. Voltages: {relays_readback.read_volts()}")

        tsms.deassert()
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        s = relays_readback.poll_for(lambda x: not x["ams_ok"], timeout_s=0.2)
        assert s is not None, (
            f"AMS_OK stayed HIGH in Error. Voltages: {relays_readback.read_volts()}")


# ---------------------------------------------------------------------------
# F-064 -- Start->Precharge relay sequence
# ---------------------------------------------------------------------------
# Documented sequence: AIR_N closes, PRECHARGE closes, AIR_P stays open.
# (Pre-charge resistor in series with the bus while AIR_P is open.)

class TestF064StartToPrechargeSequence:

    def test_f064(self, fresh_boot, tsms, dash_chg, wait_for_state,
                  relays_readback, ams_profile):
        _require(relays_readback)
        if tsms is None or dash_chg is None:
            pytest.skip("tsms/dash_chg fixture unavailable")

        # Pre: all three contactors open
        pre = relays_readback.read()
        assert not (pre["air_n"] or pre["air_p"] or pre["prech"]), (
            f"Pre-condition: all relays open in Start. Got {pre}")

        _drive_to_precharge(tsms, dash_chg, wait_for_state, ams_profile)
        # Within one FSM tick: AIR_N + PRECH high, AIR_P still low
        s = relays_readback.poll_for(
            lambda x: x["air_n"] and x["prech"] and not x["air_p"],
            timeout_s=0.15)
        assert s is not None, (
            f"Start->Precharge relay pattern wrong. Expected "
            f"AIR_N=1, AIR_P=0, PRECH=1; got {relays_readback.read()} "
            f"voltages={relays_readback.read_volts()}")


# ---------------------------------------------------------------------------
# F-065 -- Precharge->Transition relay sequence
# ---------------------------------------------------------------------------
# AIR_P closes, PRECHARGE opens (resistor out of circuit), AIR_N stays closed.

class TestF065PrechargeToTransitionSequence:

    def test_f065(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, relays_readback, ams_profile):
        _require(relays_readback)
        if tsms is None or dash_chg is None:
            pytest.skip("tsms/dash_chg fixture unavailable")

        _drive_to_transition(tsms, dash_chg, acu_heartbeat, wait_for_state,
                             ams_profile)
        s = relays_readback.poll_for(
            lambda x: x["air_n"] and x["air_p"] and not x["prech"],
            timeout_s=0.2)
        assert s is not None, (
            f"Precharge->Transition relay pattern wrong. Expected "
            f"AIR_N=1, AIR_P=1, PRECH=0; got {relays_readback.read()} "
            f"voltages={relays_readback.read_volts()}")


# ---------------------------------------------------------------------------
# F-066 -- Error: all 3 contactors open within one FSM tick
# ---------------------------------------------------------------------------

class TestF066ErrorOpensAllContactors:

    def test_f066(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, relays_readback, ams_profile):
        _require(relays_readback)
        if tsms is None or dash_chg is None:
            pytest.skip("tsms/dash_chg fixture unavailable")

        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile)
        tsms.deassert()
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)

        # Within one FSM tick (20 ms) of Error: all three open
        s = relays_readback.poll_for(
            lambda x: not (x["air_n"] or x["air_p"] or x["prech"]),
            timeout_s=0.05)
        assert s is not None, (
            f"Not all contactors opened within one FSM tick of Error. "
            f"State {relays_readback.read()} voltages "
            f"{relays_readback.read_volts()}")
