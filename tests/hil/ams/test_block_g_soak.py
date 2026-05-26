"""
Block E — Smoke / soak.

Implements E-050..E-053 from `isc-fs/IFS08-CE-AMS#123`. These tests
are long-running by design; default to the full budget per the test
plan, with `--soak-scale=N` to compress run time for CI / pre-flight
sanity (N=1 is the doc default, N=0.1 makes a 30-minute soak run for 3
minutes).

| Test  | What it checks                                          | Status      |
|-------|---------------------------------------------------------|-------------|
| E-050 | 30-minute idle soak in Start                            | implemented |
| E-051 | 30-minute Run soak                                      | implemented |
| E-052 | 50 power-cycles, each must reach Start within 2 s       | implemented |
| E-053 | Brown-out recovery (VDD drop)                           | needs PSU   |
"""

from __future__ import annotations

import os
import time

import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# CLI knob — scale soak durations for CI runs
# `--soak-scale` CLI option and the `soak_scale` fixture moved to
# `tests/hil/ams/conftest.py` so pytest registers the option during
# its conftest pass (before collection). Previously, the option was
# only registered when this file itself was collected, which made
# `pytest --soak-scale=0.1 path/to/something_else.py` reject the
# option as unknown.

# ---------------------------------------------------------------------------
# Shared soak runner
# ---------------------------------------------------------------------------

def _run_soak(observe_acu, ams_profile, *,
              minutes: float,
              expected_state: int,
              expected_state_name: str):
    """Watch 0x4A0 + 0x4A2 for `minutes` minutes. Pass iff:
      - state byte stays at `expected_state` throughout
      - inter-frame period stays within tx_telemetry_period_ms ± jitter
      - heartbeat counter is monotonic (modulo-256 wrap allowed)
      - no resets (heartbeat wouldn't snap back to ≤ 5)
    """
    period_ms  = int(ams_profile["tx_telemetry_period_ms"])
    jitter_ms  = int(ams_profile["tx_telemetry_jitter_ms"])

    deadline = time.monotonic() + minutes * 60.0
    observe_acu.clear()

    last_temps_ts: float | None = None
    last_hb: int | None = None
    bad_state_count = 0
    cadence_outliers: list[tuple[float, float]] = []
    resets_detected = 0
    frames_seen = 0

    while time.monotonic() < deadline:
        # State byte check
        fs = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
        if fs is not None:
            state = M.decode_telem_status(fs.data)["state"]
            if state != expected_state:
                bad_state_count += 1

        # Cadence + reset check (use 0x4A2 since it carries the heartbeat)
        ft = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        if ft is not None:
            hb = M.decode_telem_temps(ft.data)["heartbeat"]
            if hb != last_hb:
                frames_seen += 1
                if last_temps_ts is not None:
                    delta_ms = (ft.timestamp - last_temps_ts) * 1000.0
                    if abs(delta_ms - period_ms) > jitter_ms:
                        cadence_outliers.append((delta_ms, ft.timestamp))
                if last_hb is not None:
                    backwards = (last_hb - hb) % 256
                    if hb <= 5 and backwards > 5:
                        resets_detected += 1
                last_temps_ts = ft.timestamp
                last_hb = hb

        time.sleep(period_ms / 1000.0 / 4)

    # Diagnostics + assertions
    assert bad_state_count == 0, (
        f"FSM left {expected_state_name} {bad_state_count} times during "
        f"{minutes:.1f}-minute soak."
    )
    assert resets_detected == 0, (
        f"Detected {resets_detected} chip resets during soak — heartbeat "
        "counter snapped back to ≤ 5. Unexpected for an idle-soak run."
    )
    assert frames_seen > 0, "no telemetry seen during soak"
    assert not cadence_outliers, (
        f"{len(cadence_outliers)} of ~{frames_seen} inter-frame periods "
        f"out of {period_ms} ± {jitter_ms} ms window. First few: "
        f"{cadence_outliers[:3]}"
    )


# ---------------------------------------------------------------------------
# E-050 — 30-minute idle soak in Start
# ---------------------------------------------------------------------------

class TestE050IdleSoakInStart:

    def test_e050(self, fresh_boot, observe_acu, ams_profile, soak_scale):
        minutes = float(ams_profile["soak_idle_minutes"]) * soak_scale
        _run_soak(observe_acu, ams_profile,
                  minutes=minutes,
                  expected_state=M.FsmState.START,
                  expected_state_name="Start")


# ---------------------------------------------------------------------------
# E-051 — 30-minute Run soak
# ---------------------------------------------------------------------------

class TestE051RunSoak:

    def test_e051(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, observe_acu, ams_profile, soak_scale):
        # Drive into Run via the TSMS + DASH_CHG GPIOs (replaces the
        # retired 0x600 start_button stim per isc-fs/IFS08-CE-AMS#187).
        if tsms is None or dash_chg is None:
            pytest.skip("tsms/dash_chg fixture unavailable -- fill "
                        "tsms_tca_* / dash_chg_tca_* keys in "
                        "ams_profile.yaml once PF9/PF10 are wired "
                        "through the TCA9555.")
        tsms.assert_()
        dash_chg.assert_()
        wait_for_state(M.FsmState.PRECHARGE,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)

        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        acu_heartbeat["set_volts"](int(pack_V * 0.96))

        wait_for_state(M.FsmState.TRANSITION,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 100)
        wait_for_state(M.FsmState.RUN,
                       timeout_ms=(int(ams_profile["transition_hold_ms"]) +
                                   int(ams_profile["state_transition_window_ms"]) + 100))

        minutes = float(ams_profile["soak_run_minutes"]) * soak_scale
        _run_soak(observe_acu, ams_profile,
                  minutes=minutes,
                  expected_state=M.FsmState.RUN,
                  expected_state_name="Run")


# ---------------------------------------------------------------------------
# E-052 — 50 power-cycles, each reaches Start within 2 s
# ---------------------------------------------------------------------------

class TestE052PowerCycleResilience:

    def test_e052(self, mlc_powered, observe_acu, acu_heartbeat,
                  ams_profile, soak_scale):
        from broker.server import BrokerClient
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        cycles = max(1, int(int(ams_profile["power_cycle_count"]) * soak_scale))
        on_s   = float(ams_profile["power_cycle_on_s"])
        off_s  = float(ams_profile["power_cycle_off_s"])
        relay_bit = mlc_powered["relay_bit"]
        failures: list[tuple[int, str]] = []

        try:
            for i in range(cycles):
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=False)
                time.sleep(off_s)
                observe_acu.clear()
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=True)
                t_on = time.monotonic()

                # Wait up to 3 s (per test plan: 2 s budget) for first
                # 0x4A0. Then verify state == Start.
                deadline = t_on + 3.0
                first = None
                while time.monotonic() < deadline:
                    f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
                    if f is not None:
                        first = f
                        break
                    time.sleep(0.05)
                if first is None:
                    failures.append((i, "no telemetry within 3 s"))
                    continue
                t_first_ms = (time.monotonic() - t_on) * 1000
                if t_first_ms > 2000:
                    failures.append((i, f"first telem at +{t_first_ms:.0f} ms"))
                decoded = M.decode_telem_status(first.data)
                if decoded["state"] != M.FsmState.START:
                    failures.append((i, f"state={decoded['state_name']}"))

                # Hold powered for on_s before the next cycle.
                time.sleep(max(0.0, on_s - (time.monotonic() - t_on)))
        finally:
            # Leave the carrier powered at the end of the test so
            # downstream session-scoped teardown is clean.
            client.call("tca.write_pin", addr=0x20, port=0,
                        pin=relay_bit, value=True)
            client.close()

        assert not failures, (
            f"{len(failures)} of {cycles} power-cycles failed. First few: "
            f"{failures[:5]}"
        )


# ---------------------------------------------------------------------------
# E-053 — Brown-out recovery
# ---------------------------------------------------------------------------

class TestE053BrownOut:

    @pytest.mark.skip(reason=(
        "E-053 needs programmable VDD control to dip 3.3 V → 1.5 V → 3.3 V "
        "for 200 ms. The bench's K_n relay can only fully gate the slot "
        "supply, not modulate it; an external lab PSU or a programmable "
        "load on the slot VDD would be required."
    ))
    def test_e053_brownout_recovery(self):
        pass
