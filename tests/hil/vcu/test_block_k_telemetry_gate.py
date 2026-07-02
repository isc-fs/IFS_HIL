"""
Block K (telemetry regression gate) — feat/telemetry FDCAN3 + nRF24 + TelemetryTask
must not regress the working ECU (IFS08-CE-ECU#96).

feat/telemetry adds a 3rd CAN bus (FDCAN3 = dash, 0x510-0x51A), an nRF24 radio over
SPI1, and a TelemetryTask (200 ms, BelowNormal). Nothing is connected to FDCAN3 or
the nRF24 on the bench (the real-car scenario). This block confirms those idle /
unterminated peripherals cannot hurt the working FDCAN1/2 path, the realtime loop,
or the load-bearing AMS heartbeat. It is a REGRESSION GATE, not a feature test.

Build under test: feat/telemetry @0df7864 + ECU_HIL_STUB_START_BTN (flight-style,
ECU_STUB_* off). For K-003, FDCAN3 (kernel can1) is left DOWN so its TX bus-offs --
the "no dash node" case (see the run notes / issue #96). Durations are env-
overridable: K_SOAK_S (default 300), K_BOOT_WATCH_S (60), K_NRF_WATCH_S (15).
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from tools.firmware_test.vcu import can_map as M

K_BOOT_WATCH_S = float(os.environ.get("K_BOOT_WATCH_S", "60"))
K_SOAK_S       = float(os.environ.get("K_SOAK_S", "300"))
K_NRF_WATCH_S  = float(os.environ.get("K_NRF_WATCH_S", "15"))

# 5-task layout (feat/telemetry): control|can_rx|can_tx|telemetry|diag = 0x1F
ALL_TASKS = (M.TASK_CONTROL | M.TASK_CAN_RX | M.TASK_CAN_TX
             | M.TASK_TELEMETRY | M.TASK_DIAG)


def _latest_health(observe_acu, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_PIT_HEALTH, extended=False)
        if f is not None:
            return M.decode_pit_health(f.data)
        time.sleep(0.02)
    return None


def _hb_max_gap_ms(observe_acu, duration_s, ext):
    """Poll 0x100 for duration_s; return (max inter-frame gap ms, distinct count)."""
    stamps, last_ts = [], None
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_HEARTBEAT, extended=ext)
        if f is not None and f.timestamp != last_ts:
            stamps.append(f.timestamp)
            last_ts = f.timestamp
        time.sleep(0.002)
    gaps = [(b - a) * 1000.0 for a, b in zip(stamps, stamps[1:])]
    return (max(gaps) if gaps else 0.0, len(stamps))


class TestK001BootCleanNoResetLoop:
    def test_k001_boot_clean_no_reset_loop(self, fresh_boot, observe_acu, vcu_profile):
        """K-001: 0x100 <=1.5 s, 0x704.reset_cause=POR, and over K_BOOT_WATCH_S no
        reset loop (uptime_s climbs, reset_cause unchanged)."""
        boot_ms = (fresh_boot["t_first_frame"] - fresh_boot["t_power_on"]) * 1000.0
        assert boot_ms <= 1500.0, f"0x100 took {boot_ms:.0f} ms (> 1500 ms)"

        h0 = _latest_health(observe_acu)
        assert h0 is not None, "no 0x704 health frame (DiagTask / FDCAN2 dead?)"
        assert h0["reset_cause"] == int(M.ResetCause.POR), \
            f"reset_cause {h0['reset_cause']} != POR at boot"

        deadline = time.monotonic() + K_BOOT_WATCH_S
        prev_uptime = h0["uptime_s"]
        while time.monotonic() < deadline:
            time.sleep(2.0)
            h = _latest_health(observe_acu)
            assert h is not None, "0x704 stopped streaming -- app died / reset"
            assert h["reset_cause"] == int(M.ResetCause.POR), \
                f"reset_cause -> {h['reset_cause']} (last_fault 0x{h['last_fault']:02X}) -- a reset occurred"
            up = h["uptime_s"]
            # uptime_s is a byte (wraps 255->0). A real reset drops it toward 0 AND
            # flips reset_cause (already checked). Only a mid-range drop is a reset.
            if up + 5 < prev_uptime and prev_uptime <= 200:
                pytest.fail(f"uptime_s dropped {prev_uptime}->{up} -- reset loop")
            prev_uptime = up


class TestK002FiveTaskLiveness:
    def test_k002_five_tasks_alive_no_fault(self, fresh_boot, observe_acu):
        """K-002: 0x704 byte4 -- all 5 tasks (control/can_rx/can_tx/telemetry/diag)
        set, last_fault=0x00 (no 0xF5 StackOverflow / 0xF6 MallocFailed)."""
        seen, last_fault = 0, None
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_PIT_HEALTH, extended=False)
            if f is not None:
                h = M.decode_pit_health(f.data)
                seen |= h["task_ran_mask"]
                last_fault = h["last_fault"]
            time.sleep(0.05)
        missing = ALL_TASKS & ~seen
        assert missing == 0, \
            f"task-liveness 0x{seen:02X} missing 0x{missing:02X} " \
            f"(telemetry=0x{M.TASK_TELEMETRY:02X} diag=0x{M.TASK_DIAG:02X})"
        assert last_fault == 0x00, \
            f"last_fault 0x{last_fault:02X} = {M.LAST_FAULT.get(last_fault, '?')}"


class TestK003Fdcan3BusOffContained:
    def test_k003_fdcan3_busoff_does_not_disturb_fdcan12(self, fresh_boot, inv_heartbeat,
                                                         acu_inject, observe_acu, observe_inv,
                                                         vcu_profile):
        """K-003: with FDCAN3 (dash) unterminated -> bus-off, FDCAN1/2 TX stays
        healthy over K_SOAK_S -- 0x100 gap < 200 ms, 0x360 keeps streaming, can_tx
        liveness bit set, no IWDG. (Run with FDCAN3/can1 DOWN -- see module docstring.)"""
        inv_heartbeat["vdc_ready"]()
        acu_inject["set_precharge"](1)
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_standby"]))
        time.sleep(1.0)   # let the FSM advance + 0x360 setpoints stream

        limit = float(vcu_profile.get("vcu_stale_ms", 200))
        hb_ext = bool(vcu_profile.get("heartbeat_extended", False))
        deadline = time.monotonic() + K_SOAK_S
        last_hb_ts = None
        last_360_ts, last_360_wall = None, time.monotonic()
        worst_gap = 0.0
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_HEARTBEAT, extended=hb_ext)
            if f is not None and f.timestamp != last_hb_ts:
                if last_hb_ts is not None:
                    g = (f.timestamp - last_hb_ts) * 1000.0
                    worst_gap = max(worst_gap, g)
                    assert g < limit, f"0x100 gap {g:.0f} ms >= {limit:.0f} ms (FDCAN2 TX perturbed)"
                last_hb_ts = f.timestamp

            fi = observe_inv.last(M.ID_INV_CMD, extended=False)
            if fi is not None and fi.timestamp != last_360_ts:
                last_360_ts, last_360_wall = fi.timestamp, time.monotonic()
            assert (time.monotonic() - last_360_wall) < 1.0, \
                "0x360 inverter setpoint stopped advancing (FDCAN1 TX stalled)"

            fh = observe_acu.last(M.ID_PIT_HEALTH, extended=False)
            if fh is not None:
                h = M.decode_pit_health(fh.data)
                assert h["task_ran_mask"] & M.TASK_CAN_TX, "can_tx task bit cleared during soak"
                assert h["reset_cause"] != int(M.ResetCause.IWDG), \
                    "IWDG reset during FDCAN3 bus-off soak"
            time.sleep(0.4)


class TestK004Nrf24AbsentContained:
    def test_k004_nrf24_absent_realtime_and_no_iwdg(self, fresh_boot, observe_acu, vcu_profile):
        """K-004: nRF24 unpopulated -> the TelemetryTask SPI path must not starve the
        10 ms ControlTask (0x100 cadence holds) nor trip the IWDG, and the telemetry
        task itself must keep running (not hang on an absent chip)."""
        hb_ext = bool(vcu_profile.get("heartbeat_extended", False))
        max_gap = float(vcu_profile["heartbeat_max_gap_ms"]) * 3   # B-block 3x margin
        worst, count = _hb_max_gap_ms(observe_acu, K_NRF_WATCH_S, hb_ext)
        assert count > 10, f"only {count} 0x100 frames in {K_NRF_WATCH_S:.0f}s"
        assert worst <= max_gap, \
            f"0x100 worst gap {worst:.0f} ms > {max_gap:.0f} ms -- ControlTask starved (nRF24 SPI?)"
        h = _latest_health(observe_acu)
        assert h is not None and h["reset_cause"] != int(M.ResetCause.IWDG), \
            "IWDG fired (nRF24 wedge starved the IWDG kick?)"
        assert h["task_ran_mask"] & M.TASK_TELEMETRY, \
            "telemetry task frozen -- nRF24 SPI hang on the absent chip"


class TestK005VcuStaleContract:
    def test_k005_heartbeat_never_stale_across_states(self, fresh_boot, inv_heartbeat,
                                                     acu_inject, pedals, start_button,
                                                     observe_acu, vcu_profile):
        """K-005: 0x100 never gaps > 200 ms in ANY FSM state -- the load-bearing
        ECU<->AMS VcuStale contract must survive the telemetry task while the FSM
        transitions boot -> precharge -> R2D -> Active."""
        hb_ext = bool(vcu_profile.get("heartbeat_extended", False))
        limit = float(vcu_profile.get("vcu_stale_ms", 200))
        st = {"run": True, "last_ts": None, "worst": 0.0, "err": None}

        def sampler():
            while st["run"]:
                f = observe_acu.last(M.ID_HEARTBEAT, extended=hb_ext)
                if f is not None and f.timestamp != st["last_ts"]:
                    if st["last_ts"] is not None:
                        g = (f.timestamp - st["last_ts"]) * 1000.0
                        st["worst"] = max(st["worst"], g)
                        if g >= limit and st["err"] is None:
                            st["err"] = g
                    st["last_ts"] = f.timestamp
                time.sleep(0.002)

        t = threading.Thread(target=sampler, daemon=True)
        t.start()
        try:
            time.sleep(1.0)                                          # WaitInvVdcConfig
            inv_heartbeat["vdc_ready"](); acu_inject["set_precharge"](1)
            time.sleep(1.5)                                          # Precharge -> WaitStartBrake
            pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
            start_button["press"]()
            inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
            time.sleep(float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 3.0)   # R2dDelay -> Active
            time.sleep(1.0)
        finally:
            st["run"] = False
            time.sleep(0.1)
        assert st["worst"] > 0, "no 0x100 frames sampled during the state sweep"
        assert st["err"] is None, \
            f"0x100 gap {st['err']:.0f} ms >= {limit:.0f} ms during a state transition (VcuStale risk)"
