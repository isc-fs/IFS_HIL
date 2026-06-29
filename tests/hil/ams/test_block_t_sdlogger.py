"""
Block T — microSD logger correctness (#408).

The logger is write-only to the card: g_log_state / g_log_rows / g_log_files
are internal (sd_log_stats() is never emitted on CAN, and #406 LOGFS isn't
landed). So the log CONTENT (314-col CSV, 4 MiB rotation, sealed-parse,
power-loss durability) needs a physical card pull or #406. What IS
CAN-observable is the safety-relevant guarantee: the best-effort logger must
NEVER disturb the 10 ms MainTask / telemetry cadence / IWDG.

| ID    | Check | Runs on |
|-------|-------|---------|
| T-150 | logger active (card in) does NOT perturb FSM / telem cadence / IWDG (soak) | std rig |
| T-151 | content: LOGnnnn.TMP grows, 314-col header, ~4 Hz rows, 4 MiB rotation, sealed .CSV parses | CARD PULL |
| T-152 | power-loss mid-write -> remounts clean, FS not corrupted | CARD PULL + power |

Logger config (ams_config.hpp): 4 Hz (LogSamplePeriodMs=250), 4 MiB rotation
(LogFileMaxBytes), LOG%04lu.TMP->.CSV, f_sync every 1 s (the power-cut bound).
"""

from __future__ import annotations

import os
import time

import pytest

from tools.firmware_test.ams import can_map as M

_SOAK_S = float(os.environ.get("AMS_SD_LOG_SOAK_S", "20"))
_CARD_PULL = os.environ.get("AMS_SD_CARD_PULL")    # operator will soak, pull + read the card


def _gaps(frames):
    ts = sorted(f.timestamp for f in frames)
    g = [b - a for a, b in zip(ts, ts[1:])]
    return (max(g, default=0.0), (sum(g) / len(g) if g else 0.0))


class TestBlockTSdLogger:

    # T-150 -- the logger must not perturb the realtime path ----------------
    def test_t150_logger_no_maintask_impact(self, fresh_boot, wait_for_settled,
                                            observe_acu):
        snap = wait_for_settled()
        assert snap["state"] == M.FsmState.START and snap["ams_ok"]
        observe_acu.clear()
        time.sleep(_SOAK_S)
        frames = observe_acu.frames(M.ID_TELEM_STATUS)
        n_min = int(_SOAK_S * 1000 / M.TX_TELEM_PERIOD_MS) - 3
        assert len(frames) >= n_min, \
            f"0x4A0 dropped ({len(frames)} vs ~{n_min+3}) -- logger perturbing telemetry?"
        gap, avg = _gaps(frames)
        # the 10 ms MainTask owns telemetry; the best-effort logger (card writes,
        # f_sync, rotation) must never block it.
        assert gap < 1.0, (
            f"0x4A0 max gap {gap*1000:.0f}ms (avg {avg*1000:.0f}ms) over the soak -- "
            "the logger must stay off the realtime path (#408, safety-relevant).")
        end = M.decode_telem_status(observe_acu.last(M.ID_TELEM_STATUS).data)
        assert end["state"] == M.FsmState.START and end["ams_ok"], \
            "FSM / AMS_OK perturbed during the logger soak"

    # T-151 -- log content (operator pulls + reads the card) ----------------
    @pytest.mark.skipif(not _CARD_PULL,
        reason="logger state isn't on CAN. Set AMS_SD_CARD_PULL=1, let it soak, then pull "
               "the card + read LOGnnnn.CSV: 314-col header (c<m>_<n> cell mV, t<m>_<n> "
               "temps, scalars), ~4 Hz rows with real values, rotation at 4 MiB "
               "(.TMP sealed -> .CSV), sealed file parses cleanly. (#406 makes this "
               "CAN-observable once it lands.)")
    def test_t151_log_content_manual(self):
        pytest.skip("manual card-read step -- see the skip reason")

    # T-152 -- power-loss durability (operator) -----------------------------
    @pytest.mark.skipif(not _CARD_PULL,
        reason="Set AMS_SD_CARD_PULL=1: pull power mid-write, re-boot, read the card. "
               "f_sync every 1 s bounds the loss; the FS must remount clean with no "
               "corruption (sealed files intact, active .TMP truncated cleanly).")
    def test_t152_power_loss_durability_manual(self):
        pytest.skip("manual power-loss step -- see the skip reason")
