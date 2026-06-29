"""
Block S — microSD boot-safety (#407).

The acceptance gate that an absent / dead / bad-FS card can NEVER brick the
AMS: it must boot, run the safety FSM, stream telemetry, refresh the IWDG,
and keep the BL/CAN recovery path in every card state.

The decoupling fix is already on feat/microsd-fatfs: the SDMMC *hardware*
init is off the boot path (main.c: MX_IWDG1_Init -> MX_FATFS_Init ->
osKernelStart, no MX_SDMMC1_SD_Init / f_mount before the scheduler). SD
bring-up is a non-fatal f_mount in the low-priority SdLoggerTask, gated on
card-detect (PE3). So these should pass GREEN, not boot-loop.

| ID    | Card state | Runs on |
|-------|-----------|---------|
| S-140 | card in: boots, safety runs, AMS_OK high post-grace          | std rig |
| S-141 | card in: NO boot-loop -- telemetry streams unbroken (soak)   | std rig |
| S-144 | card in: boot-time bound -- first telem within grace+slack   | std rig |
| S-142 | NO card: 0x002 still reaches the BL (reflash path survives)  | OPERATOR |
| S-143 | NO card: boots, runs safety, no boot-loop                    | OPERATOR |

S-142/S-143 need a bench operator to run with the card physically removed
(set AMS_SD_NOCARD=1). The card-in cases prove the decoupled boot path; the
no-card cases prove the absent-card path on silicon -- that's the brick #407
gates.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from tools.firmware_test.ams import can_map as M

_NOCARD = os.environ.get("AMS_SD_NOCARD")          # operator pulled the card
_SOAK_S = float(os.environ.get("AMS_SD_BOOT_SOAK_S", "12"))


def _max_gap(frames) -> float:
    ts = sorted(f.timestamp for f in frames)
    return max((b - a for a, b in zip(ts, ts[1:])), default=0.0)


class TestBlockSSdBootSafety:

    # S-140 -----------------------------------------------------------------
    def test_s140_card_in_boots_safety_runs(self, fresh_boot, wait_for_settled):
        snap = wait_for_settled()
        assert snap["state"] == M.FsmState.START
        assert snap["ams_ok"], "AMS_OK must be high once healthy + past grace (card in)"

    # S-141 -- no boot-loop -------------------------------------------------
    def test_s141_no_boot_loop_telemetry_unbroken(self, fresh_boot,
                                                  wait_for_settled, observe_acu):
        wait_for_settled()
        observe_acu.clear()
        time.sleep(_SOAK_S)
        frames = observe_acu.frames(M.ID_TELEM_STATUS)
        n_min = int(_SOAK_S * 1000 / M.TX_TELEM_PERIOD_MS) - 3
        assert len(frames) >= n_min, \
            f"only {len(frames)} 0x4A0 in {_SOAK_S}s (expected ~{n_min+3}) -- telemetry stalled?"
        gap = _max_gap(frames)
        assert gap < 3.0, (
            f"0x4A0 gap {gap:.1f}s over the soak -- a boot-loop/IWDG reset blanks "
            "telemetry for ~the boot grace. The SD boot path must not stall (#407).")

    # S-144 -- boot-time bound ----------------------------------------------
    def test_s144_boot_time_no_sd_stall(self, fresh_boot):
        dt = fresh_boot["t_first_frame"] - fresh_boot["t_power_on"]
        assert dt < 4.0, (
            f"first telemetry {dt:.1f}s after power-on -- the boot-path SD init must "
            "not stall (#407). Expect ~2.5s (boot grace 2s + telem + slack).")

    # S-142 -- BL recovery with NO card (operator-gated) --------------------
    @pytest.mark.skipif(not _NOCARD,
        reason="set AMS_SD_NOCARD=1 with the card OUT: the 0x002 trigger must still "
               "reach the BL so a reflash survives an absent/dead card (#407).")
    def test_s142_bl_recovery_no_card(self, fresh_boot, wait_for_settled):
        from tools.flash_ams_via_trigger import send_trigger, discover_bl
        wait_for_settled()
        send_trigger()
        time.sleep(1.0)
        assert discover_bl(), \
            "BL not reachable after 0x002 with no card -- the recovery path broke"
        # leaves the chip in BL; the next test's fresh_boot power-cycles -> auto-jump back

    # S-143 -- no card boots (operator-gated) -------------------------------
    @pytest.mark.skipif(not _NOCARD,
        reason="set AMS_SD_NOCARD=1 with the card OUT: the AMS must boot + run safety + "
               "stream telemetry with no boot-loop -- the brick #407 gates.")
    def test_s143_no_card_boots(self, fresh_boot, wait_for_settled, observe_acu):
        snap = wait_for_settled()
        assert snap["state"] == M.FsmState.START
        assert snap["ams_ok"], "AMS_OK must be high with NO card -- absent card must not fault"
        observe_acu.clear()
        time.sleep(_SOAK_S)
        assert _max_gap(observe_acu.frames(M.ID_TELEM_STATUS)) < 3.0, \
            "no-card boot-loop: 0x4A0 telemetry gapped (the #407 brick)"
