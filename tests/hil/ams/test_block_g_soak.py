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
                    # Heartbeat is a monotonic +1 counter (mod 256), so a
                    # normal step -- including the 255->0 wrap -- has a
                    # forward delta of 1. A chip reset snaps it back to 0,
                    # i.e. a large forward delta. (The old check computed
                    # `(last_hb - hb) % 256`, which is 255 for a normal +1
                    # step, so it mis-flagged every wrap as ~6 resets.)
                    forward = (hb - last_hb) % 256
                    if forward > 5:
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
    # Tolerate rare isolated cadence blips: an RTOS scheduling delay can
    # nudge one 500 ms telemetry frame past the ±20 ms window over a
    # 30-min soak (frames are kernel-timestamped, so these are real but
    # harmless). A-008 covers tight cadence over 60 s; here we only flag
    # a systematic drift -- more than 0.5 % of frames out of window.
    max_outliers = max(3, frames_seen // 200)
    assert len(cadence_outliers) <= max_outliers, (
        f"{len(cadence_outliers)} of ~{frames_seen} inter-frame periods "
        f"out of {period_ms} ± {jitter_ms} ms ({max_outliers} allowed). "
        f"First few: {cadence_outliers[:3]}"
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
        # Use Block C's maintained drive helper (TSMS held + a #316 DASH_CHG
        # momentary press, then RC-ramp the bus to Run) -- same path as
        # F-076/F-080. The old inline held-DASH model no longer fires the
        # gate on v1.6.0 (#316: DASH_CHG is edge-detected, not a level).
        from tests.hil.ams.test_block_c_fsm import _drive_to_run
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile)

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


# E-053 (brown-out recovery) dropped per AMS #272 — needs programmable
# VDD control (3.3 V → 1.5 V → 3.3 V dip), and the bench K-relay is
# on/off only. Requires lab PSU or programmable load on slot VDD; not
# in the production bench architecture.


# ---------------------------------------------------------------------------
# G-097 — Boot diag via pit-diag 0x6C4  (NEW per AMS #272)
# ---------------------------------------------------------------------------

class TestG097BootDiag:
    """Per AMS #272 G-097: after a cold boot, pit-diag `0x6C4` reports:
      bytes [0..3]  jump_reason (LE u32) = 0 (cold boot, no magic in BKP2R)
      byte  [4]     g_app_init_progress = 7 (App_InitTask reached self-exit)
      bytes [5..7]  g_fdcan1_start_result = 0 (HAL_OK)

    A non-zero g_fdcan1_start_result or app_init_progress < 7 means
    boot didn't complete cleanly and any further test on this chip is
    suspect — useful as a per-test boot-health smoke check.
    """

    def test_g097_boot_diag(self, fresh_boot, pit_diag):
        boot = pit_diag.wait_for(M.ID_PIT_DIAG_BOOT)
        assert len(boot) == 8, f"0x6C4 dlc != 8 (got {len(boot)})"

        jump_reason = int.from_bytes(boot[0:4], "little")
        app_progress = boot[4]
        fdcan_start = int.from_bytes(boot[5:8], "little")

        assert jump_reason == 0, (
            f"0x6C4[0..3] jump_reason = 0x{jump_reason:08X} on cold boot; "
            "expected 0 (no JumpReason magic in BKP2R). "
            "If non-zero, prior reboot wasn't a clean cold cycle.")
        assert app_progress == 7, (
            f"0x6C4[4] g_app_init_progress = {app_progress}; "
            "expected 7 (App_InitTask reached self-exit). Lower values "
            "indicate where init stalled (see app_init_task.cpp).")
        assert fdcan_start == 0, (
            f"0x6C4[5..7] g_fdcan1_start_result = 0x{fdcan_start:06X}; "
            "expected 0 (HAL_OK). Non-zero means HAL_FDCAN_Start failed.")


# ---------------------------------------------------------------------------
# G-102 — Per-IC PEC localisation  (NEW per AMS #272)
# ---------------------------------------------------------------------------

class TestG102PerIcPecLocalisation:
    """Per AMS #272 G-102: Pico fails PEC on chain index N → only the
    `0x6C7[N]` (or `0x6C8[N-8]`) byte climbs while others stay at 0.

    Identical assertion to Block E's E-067, but framed under Block G
    (pit-diag stream) so the test reads as "verify the pit-diag stream
    correctly localises a per-IC fault" rather than "verify the chain
    integrity check". Same Pico STOP_REPLY mechanism.

    See `tests/hil/ams/test_block_e_ltc.py::TestE067PerIcPecLocalisation`
    for the Block E twin. Same caveat re: IFS08_HIL#44 saturating the
    baseline.
    """

    def test_g102_pec_localisation(self, fresh_boot, pico_emu, pit_diag):
        # Baseline.
        pit_diag.wait_for_scan()
        a0 = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_A)
        b0 = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_B)
        baseline = list(a0[0:8]) + list(b0[0:2])

        try:
            # Silence module 1 (chain index 2 + 3) this time — distinct
            # from the E-067 module-2 case so the two tests exercise
            # different chain slots in the same suite.
            pico_emu.stop_reply(0x02)
            time.sleep(2.0)

            a1 = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_A)
            b1 = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_B)
            current = list(a1[0:8]) + list(b1[0:2])
            delta = [(c - b) & 0xFF for c, b in zip(current, baseline)]

            assert delta[2] > 0, f"chain index 2 PEC count did not climb (Δ={delta[2]})"
            assert delta[3] > 0, f"chain index 3 PEC count did not climb (Δ={delta[3]})"

            silenced = {2, 3}
            collateral = [(i, d) for i, d in enumerate(delta)
                          if i not in silenced and d > 5]
            assert not collateral, (
                "Per-IC PEC count climbed for chain indices outside the "
                f"silenced module-1 pair (2, 3): {collateral}. "
                "If IFS08_HIL#44 is open the baseline is saturated and "
                "this assertion can't fire cleanly; fix the bench first.")
        finally:
            pico_emu.resume_all()
