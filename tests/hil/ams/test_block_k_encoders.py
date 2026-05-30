"""
Block K -- telemetry encoder correctness.

Host-side regression guards for the AMS telemetry decoders + their
build-specific layout. These tests don't drive any new hardware; they
just verify the decoder we trust today still agrees with the firmware
output. Per `isc-fs/IFS08-CE-AMS#193`.

| Test  | What it checks                                                   | Status      |
|-------|------------------------------------------------------------------|-------------|
| K-100 | 0x4A0 decode matches stub seed in Start/Precharge/Run/Error      | implemented |
| K-101 | 0x4A1 LE pack_voltage_mV + filtered_mA across a DAC sweep        | deferred    |
| K-102 | 0x4A2 layout split (flight vs HIL-STUB) -- bench reads HIL       | implemented |
| K-103 | 0x4A2[7] heartbeat strictly monotonic mod-256 over 1000 frames   | implemented |
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Local helpers (avoid cross-file imports of Block C internals)
# ---------------------------------------------------------------------------

def _require_inputs(tsms, dash_chg):
    missing = []
    if tsms     is None: missing.append("tsms_*")
    if dash_chg is None: missing.append("dash_chg_*")
    if missing:
        pytest.skip(f"Pin fixture(s) unavailable: {' + '.join(missing)} "
                    "keys absent from ams_profile.yaml.")


def _wait_state(observe_acu, target_state: int, timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
        if f is not None:
            decoded = M.decode_telem_status(f.data)
            if decoded["state"] == target_state:
                return decoded
        time.sleep(0.05)
    raise TimeoutError(
        f"FSM did not reach {M.FsmState.name(target_state)} within "
        f"{timeout_s:.1f} s")


# ---------------------------------------------------------------------------
# K-100 -- 0x4A0 decode internal consistency across all 6 FSM states
# ---------------------------------------------------------------------------
# For every state we touch in a car-path drive, the decoded fields
# must agree with the stub seeder constants we trust elsewhere
# (module_online_mask = 0x1F, min/max cell mV = stub_cell_mV).
# State byte must be one of 0..5. AMS_OK must be 1 in Start->Run and
# 0 in Error.

class TestK100Frame0x4A0DecodeAcrossStates:

    def test_k100(self, fresh_boot, tsms, dash_chg, acu_heartbeat,
                  wait_for_state, observe_acu, ams_profile):
        _require_inputs(tsms, dash_chg)
        stub_mask = int(ams_profile["stub_module_online_mask"])
        stub_mv   = int(ams_profile["stub_cell_mV"])

        # Capture one 0x4A0 per state visited and assert the invariants.
        seen: dict[int, dict] = {}

        def snapshot(state: int):
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            assert f is not None, f"no 0x4A0 at {M.FsmState.name(state)}"
            d = M.decode_telem_status(f.data)
            assert d["state"] == state, (
                f"expected state {M.FsmState.name(state)} but decoder "
                f"returned {d['state_name']} (raw=0x{d['state']:02X})")
            assert d["module_online_mask"] == stub_mask, (
                f"{d['state_name']}: module_online_mask=0x{d['module_online_mask']:02X}, "
                f"expected 0x{stub_mask:02X}")
            assert d["min_cell_mV"] == stub_mv and d["max_cell_mV"] == stub_mv, (
                f"{d['state_name']}: cell_mV min/max = "
                f"{d['min_cell_mV']}/{d['max_cell_mV']}, expected {stub_mv}")
            seen[state] = d

        # The first 0x4A0 after boot carries partial-first-poll cells
        # (modules not all read yet) and AMS_OK hasn't asserted (it waits
        # for grace + first full poll, ~4.4 s -- AMS #301). Wait for both
        # to settle before snapshotting Start.
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            f0 = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f0 is not None:
                d0 = M.decode_telem_status(f0.data)
                if d0["min_cell_mV"] == stub_mv and d0["ams_ok"]:
                    break
            time.sleep(0.1)

        # Start
        snapshot(M.FsmState.START)
        assert seen[M.FsmState.START]["ams_ok"], "AMS_OK should be 1 in Start"

        # Precharge
        tsms.assert_(); dash_chg.assert_()
        wait_for_state(M.FsmState.PRECHARGE,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        snapshot(M.FsmState.PRECHARGE)

        # Run -- Transition is a single-tick passthrough (#244) the 20 ms
        # poll can't observe, so we don't snapshot it; ramp the bus and
        # wait for Run directly, like Block C/F `_drive_to_run`.
        pack_V = int(ams_profile["stub_expected_pack_mV"]) // 1000
        acu_heartbeat["ramp_to"](pack_V)
        hold_ms = int(ams_profile["transition_hold_ms"])
        wait_for_state(M.FsmState.RUN,
                       timeout_ms=hold_ms + int(ams_profile["state_transition_window_ms"]) + 100)
        snapshot(M.FsmState.RUN)

        # Error (drop TSMS)
        tsms.deassert()
        wait_for_state(M.FsmState.ERROR,
                       timeout_ms=int(ams_profile["state_transition_window_ms"]) + 50)
        snapshot(M.FsmState.ERROR)
        assert not seen[M.FsmState.ERROR]["ams_ok"], "AMS_OK should be 0 in Error"

        # We didn't hit Charge in this run (Error is sticky after Run).
        # C-042's charger-path test covers the Charge-state decoder
        # invariants; not repeating here to keep the test fast.


# ---------------------------------------------------------------------------
# K-101 -- 0x4A1 pack + current across DAC sweep
# ---------------------------------------------------------------------------
# Requires `current_heartbeat` DAC stim on the new PF7 location (was
# PF11 before isc-fs/IFS08-CE-AMS#189). Profile keys need an update
# before this can drive a real sweep -- see issue #193 "Test-
# infrastructure dependencies".

class TestK101PackAndCurrentSweep:

    @pytest.mark.skip(reason=(
        "K-101 needs `current_heartbeat` rewired to PF7 / ADC3_INP3 per "
        "isc-fs/IFS08-CE-AMS#189. The current fixture targets PF11 and "
        "yields None when keys are absent. Re-enable after the bench "
        "DAC4 ch0 -> PF7 jumper is in and the profile is updated."
    ))
    def test_k101(self):
        pass


# ---------------------------------------------------------------------------
# K-102 -- HIL_STUB layout active (sentinel bit on byte 5)
# ---------------------------------------------------------------------------
# 0x4A2[5] carries the cockpit inputs with bit 7 set as a sentinel (the
# unified encoder, since AMS #284 retired the HIL-stub-vs-flight split).
# A missing sentinel means the cockpit byte isn't being packed -- a
# corrupt or pre-#284 binary -- so fail loudly rather than spend hours
# debugging "why doesn't the cockpit fixture do anything".

class TestK102HilLayoutActive:

    def test_k102_cockpit_byte_sentinel_set(self, fresh_boot, observe_acu,
                                            ams_profile):
        time.sleep(int(ams_profile["tx_telemetry_period_ms"]) / 1000.0 + 0.2)
        f = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert f is not None, "no 0x4A2 after settle"
        cb = M.decode_telem_temps(f.data)["cockpit"]
        assert cb["valid"], (
            f"cockpit sentinel (bit 7) not set: byte5=0x{cb['raw']:02X}. "
            f"The cockpit byte isn't being packed -- corrupt or pre-#284 "
            f"binary on the bench; rebuild from dev and reflash."
        )


# ---------------------------------------------------------------------------
# K-103 -- heartbeat counter monotonic mod-256 over 1000 frames
# ---------------------------------------------------------------------------
# A-007 already checks short-window heartbeat increment; this is the
# wraparound + no-skipped-frames guard over a much longer window.
# 1000 frames * 500 ms = 500 s (~8.5 min). Guarded by `--soak-scale`
# for CI runs so off-bench dry-runs don't burn an hour.

class TestK103HeartbeatMonotonic:

    def test_k103(self, fresh_boot, observe_acu, ams_profile, soak_scale):
        target_frames = max(20, int(1000 * soak_scale))
        period_ms     = int(ams_profile["tx_telemetry_period_ms"])
        # Generous total timeout: 1.5x the expected ideal duration.
        deadline = time.monotonic() + (target_frames * period_ms / 1000.0) * 1.5

        observe_acu.clear()
        seen = 0
        last_hb: int | None = None
        skips: list[tuple[int, int, int]] = []  # (frame_idx, last_hb, hb)
        backwards: list[tuple[int, int, int]] = []

        while seen < target_frames and time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
            if f is None:
                time.sleep(period_ms / 1000.0 / 4)
                continue
            hb = M.decode_telem_temps(f.data)["heartbeat"]
            if hb == last_hb:
                time.sleep(period_ms / 1000.0 / 4)
                continue
            if last_hb is not None:
                # Expected increment is exactly 1 mod 256.
                delta = (hb - last_hb) % 256
                if delta != 1:
                    if delta > 128:
                        # Counter went backwards -- chip reset?
                        backwards.append((seen, last_hb, hb))
                    else:
                        # Skip ahead (lost frames).
                        skips.append((seen, last_hb, hb))
            last_hb = hb
            seen += 1
            time.sleep(period_ms / 1000.0 / 4)

        assert seen >= target_frames * 0.95, (
            f"only saw {seen} heartbeats of {target_frames} target before "
            f"timeout. Telemetry may be gapping.")
        assert not backwards, (
            f"heartbeat went backwards {len(backwards)} times -- chip "
            f"reset detected. First few: {backwards[:3]}"
        )
        assert not skips, (
            f"heartbeat skipped {len(skips)} times (delta != 1 mod 256). "
            f"First few (frame_idx, prev, now): {skips[:3]}"
        )
