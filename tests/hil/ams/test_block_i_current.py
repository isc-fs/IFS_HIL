"""
Block I/J — pack-current sensor (AMS #348 differential rework).

The #348 firmware reads pack current as an ADC3 DIFFERENTIAL pair PF7/PF8
(INP3/INN3), ±5 mV/A, replacing the single-ended Hall path. The bench drives
the pair via the `pack_current_diff` fixture (DAC4 ch0 -> S_current_p/PF7,
ch1 -> S_current_n/PF8) and reads the firmware's decoded current back on
0x4A1 `filtered_mA` (same field the firmware also ships as 0x135 accu). Block
I covers the measurement; Block J the over-current safety trip.

| ID    | What it checks                                                  | Status |
|-------|-----------------------------------------------------------------|--------|
| I-100 | reported filtered_mA tracks injected A (linearity + sign)       | impl   |
| I-101 | 0 A injection reads ~0 (no differential offset)                 | impl   |
| J-100 | |current| > CurrentMaxMa -> Error (unconditional, undebounced)  | impl   |
| J-101 | over-current latch is sticky (drop to 0 A, stays Error)         | impl   |

Notes:
  - Characterized over 6 boots (2026-06-06): zero is STABLE at +0.6 A
    (stdev 0.1 A) and the gain is consistently 0.924 (reads ~7.6% LOW vs the
    5 mV/A injection). I-100's tolerance (`pack_current_accuracy_tol_pct`,
    default 12 %) accommodates the gain; tighten if/when the AMS team blesses a
    tighter `adc_to_mA` / VREF+.
  - The IIR current filter needs ~3-4 s to settle a big step (`pack_current_settle_s`).
    Read too soon (1.5 s) off a 200 A step and the residual reads ~-22 A — a
    settle artifact, NOT an offset. The sweep is ordered to keep steps <= 200 A.
  - Over-current injection (`pack_current_overcurrent_a`, default 240 A) sits
    well above CurrentMaxMa (200 A) even after the 7.6% under-read
    (240 * 0.924 ~= 222 A > 200 A), and the legs stay in-rail (CM 1.44 V).
  - `pack_current_diff` refreshes at 20 Hz, so CurrentStale never pre-empts the
    over-limit trip; `fresh_boot_diff` keeps that source active across boot.
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M


def _require_diff(pack_current_diff):
    """Skip when the differential current fixture is disabled (DAC routing
    keys absent from ams_profile.yaml)."""
    if not pack_current_diff.get("enabled"):
        pytest.skip("pack_current_diff disabled: pack_current_dac_idx + "
                    "pack_current_dac_ch_p/_n absent from ams_profile.yaml.")


def _read_filtered_mA(observe_acu):
    """Latest 0x4A1 filtered_mA (signed mA, + discharge / - charge), or None."""
    f = observe_acu.last(M.ID_TELEM_PACK, extended=False)
    return None if f is None else M.decode_telem_pack(f.data)["filtered_mA"]


def _read_state(observe_acu):
    f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
    return None if f is None else M.decode_telem_status(f.data)["state"]


def _past_boot_grace(fresh_boot_diff, ams_profile, margin_s=0.4):
    """Block until the boot grace has elapsed — faults are suppressed during
    it, so an over-current injected before grace-end wouldn't trip."""
    grace_s = int(ams_profile["boot_grace_ms"]) / 1000.0
    elapsed = time.monotonic() - fresh_boot_diff["t_power_on"]
    if elapsed < grace_s + margin_s:
        time.sleep(grace_s + margin_s - elapsed)


# ---------------------------------------------------------------------------
# Block I — pack-current measurement
# ---------------------------------------------------------------------------

class TestBlockICurrentAccuracy:

    def test_i100_reported_current_tracks_injection(self, pack_current_diff,
                                                    observe_acu, ams_profile):
        """I-100: drive a current sweep through the differential pair and
        assert the firmware's filtered_mA tracks it in sign and magnitude.
        Sweep stays strictly under CurrentMaxMa so it never trips Block J."""
        _require_diff(pack_current_diff)
        tol_pct = float(ams_profile.get("pack_current_accuracy_tol_pct", 12.0))
        settle_s = float(ams_profile.get("pack_current_settle_s", 4.0))

        observed = []
        # Monotonic order keeps point-to-point steps <= 200 A so the IIR
        # current filter fully settles within settle_s. (A 1.5 s settle off a
        # 200 A step under-reads badly: the -200 A -> 0 A residual reads ~-22 A.)
        for amps in (-200, -100, 100, 200):
            pack_current_diff["set_A"](amps)
            time.sleep(settle_s)
            mA = _read_filtered_mA(observe_acu)
            assert mA is not None, "no 0x4A1 telemetry — app TX alive?"
            observed.append((amps, mA))
        pack_current_diff["set_A"](0)

        for amps, mA in observed:
            inj_mA = amps * 1000
            assert (mA > 0) == (amps > 0), (
                f"sign mismatch at {amps:+d} A: read {mA/1000:.1f} A "
                "(+ should read as discharge)")
            err_pct = abs(abs(mA) - abs(inj_mA)) / abs(inj_mA) * 100.0
            assert err_pct <= tol_pct, (
                f"{amps:+d} A injected -> {mA/1000:.1f} A reported "
                f"({err_pct:.1f}% off, tol {tol_pct:.0f}%). NB ~7.4%-low is the "
                "known #348 gain pending adc_to_mA confirmation.")

    def test_i101_zero_injection_reads_zero(self, pack_current_diff,
                                            observe_acu, ams_profile):
        """I-101: legs equal (0 A differential) -> filtered_mA ~ 0 (no offset)."""
        _require_diff(pack_current_diff)
        floor_a = float(ams_profile.get("pack_current_zero_floor_a", 10.0))
        pack_current_diff["set_A"](0)
        time.sleep(float(ams_profile.get("pack_current_settle_s", 4.0)))
        mA = _read_filtered_mA(observe_acu)
        assert mA is not None, "no 0x4A1 telemetry"
        assert abs(mA) < floor_a * 1000, (
            f"0 A injection read {mA/1000:.1f} A — differential offset exceeds "
            f"the {floor_a:.0f} A floor.")


# ---------------------------------------------------------------------------
# Block J — over-current safety trip
# ---------------------------------------------------------------------------

class TestBlockJOverCurrent:

    def test_j100_overcurrent_trips_error(self, fresh_boot_diff, pack_current_diff,
                                          wait_for_state, observe_acu, ams_profile):
        """J-100: |filtered_mA| > CurrentMaxMa -> Error. The predicate is
        unconditional (safety_predicates.hpp: current check is not state-gated)
        and undebounced, so it trips from Start within ~one telemetry cycle."""
        _require_diff(pack_current_diff)
        assert fresh_boot_diff["first_frame"]["state"] == M.FsmState.START, \
            "expected a clean Start after fresh_boot_diff"
        _past_boot_grace(fresh_boot_diff, ams_profile)

        # Baseline: at 0 A the chip is not faulted.
        pack_current_diff["set_A"](0)
        time.sleep(0.6)
        assert _read_state(observe_acu) == M.FsmState.START, \
            "chip should still be in Start at 0 A before the over-current inject"

        # Inject over-limit -> Error.
        oc_a = float(ams_profile.get("pack_current_overcurrent_a", 240.0))
        pack_current_diff["set_A"](oc_a)
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 4 + 500)
        pack_current_diff["set_A"](0)

    def test_j101_overcurrent_latch_is_sticky(self, fresh_boot_diff,
                                              pack_current_diff, wait_for_state,
                                              observe_acu, ams_profile):
        """J-101: once tripped, removing the over-current does NOT clear Error —
        ErrorLatch is RTC-backed/sticky (only a fresh boot clears it)."""
        _require_diff(pack_current_diff)
        assert fresh_boot_diff["first_frame"]["state"] == M.FsmState.START
        _past_boot_grace(fresh_boot_diff, ams_profile)

        oc_a = float(ams_profile.get("pack_current_overcurrent_a", 240.0))
        pack_current_diff["set_A"](oc_a)
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["tx_telemetry_period_ms"]) * 4 + 500)

        # Remove the stimulus; latch must hold across several telemetry cycles.
        pack_current_diff["set_A"](0)
        time.sleep(max(1.0, int(ams_profile["tx_telemetry_period_ms"]) * 3 / 1000.0))
        st = _read_state(observe_acu)
        assert st == M.FsmState.ERROR, (
            "over-current latch should be sticky — chip recovered to "
            f"{M.FsmState.name(st)} after current returned to 0 A.")
