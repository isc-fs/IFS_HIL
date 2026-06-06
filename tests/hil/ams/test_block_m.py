"""
Block M — FreeRTOS/HAL regen regression smoke (AMS #348 / #350).

The #348 current-sensor PR rode in on a CubeMX regen that bumped FreeRTOS +
the HAL (dropped legacy cmsis_os2.h, needed a freertos.c hook-signature fix to
link). That regen landed on dev *after* #317 was last fully run, so it's
unproven against the acceptance plan. Block M is the fast smoke that the
scheduler, the producer tasks, and the new dual ADC calibration all survived
the bump; M-043 (the heavy row) is the full #317 A-G re-run, driven from the
suite runner -- not a test here.

| ID    | What it checks                                                  | Status |
|-------|-----------------------------------------------------------------|--------|
| M-040 | boots to Start, 0x4A0 holds cadence, no boot loop               | impl   |
| M-041 | MainTask / current / BMS poll / ACU CAN all producing telemetry | impl   |
| M-042 | dual ADC cal completes (no Error_Handler hang), current live    | impl   |

Run against the post-#350 dev tip (9a9d4dc; tree-identical to 42e0cfa).
"""

from __future__ import annotations

import subprocess
import pytest

from tools.firmware_test.ams import can_map as M


def _count_ids(bus, window_s):
    out = subprocess.run(["bash", "-c", f"timeout {window_s} candump {bus}"],
                         capture_output=True, text=True).stdout
    d = {}
    for l in out.splitlines():
        p = l.split()
        if len(p) >= 2:
            d[p[1].upper()] = d.get(p[1].upper(), 0) + 1
    return d


class TestBlockMRegenRegression:

    def test_m040_boot_scheduler_cadence(self, fresh_boot_diff, ams_profile):
        """M-040: the regen reaches a clean Start (scheduler ran, MainTask
        alive), and 0x4A0 holds its cadence over a window -- no boot loop."""
        assert fresh_boot_diff["first_frame"]["state"] == M.FsmState.START, \
            "regen did not reach a clean Start"
        period = int(ams_profile["tx_telemetry_period_ms"])
        window = 6.0
        n = _count_ids(ams_profile["bus_acu"], window).get("4A0", 0)
        expected = int(window * 1000 / period)
        assert n >= expected - 2, (
            f"0x4A0 cadence {n}/{expected} over {window:.0f}s -- scheduler "
            "stalled or boot-looping after the FreeRTOS/HAL bump.")

    def test_m041_all_tasks_producing(self, fresh_boot_diff, ams_profile):
        """M-041: every producer task is alive -- MainTask (0x4A0), current
        sensor (0x4A1), BMS poll (0x4A2 temps + 0x12C min-cell), ACU CAN
        (0x135 currents). A silent ID means the regen dropped a task."""
        d = _count_ids(ams_profile["bus_acu"], 4.0)
        required = {
            "4A0": "MainTask status", "4A1": "pack + current",
            "4A2": "BMS temps", "135": "ACU currents", "12C": "min-cell-V",
        }
        missing = [f"0x{k} ({v})" for k, v in required.items() if d.get(k, 0) == 0]
        assert not missing, f"silent producer(s) after regen: {', '.join(missing)}"

    def test_m042_adc_dualcal_and_readback(self, fresh_boot_diff, observe_acu,
                                           ams_profile):
        """M-042: the dual ADC cal (single-ended + differential) completed at
        task entry -- no Error_Handler hang (telemetry present) -- and the
        differential pack-current read-back is live + sane (~0 A) at rest."""
        f = observe_acu.last(M.ID_TELEM_PACK, extended=False)
        assert f is not None, ("no 0x4A1 -- current task hung (ADC cal stuck "
                               "in Error_Handler?)")
        mA = M.decode_telem_pack(f.data)["filtered_mA"]
        floor = float(ams_profile.get("pack_current_zero_floor_a", 5.0))
        assert abs(mA) < floor * 1000, (
            f"pack current reads {mA/1000:.1f} A at rest -- differential cal "
            "off after the regen (#350 expects ~0 A).")
