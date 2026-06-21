"""
Block B — FDCAN bring-up resilience (IFS08-CE-ECU#71). 🎯 the #48 regression gate.

#48 was a disconnected DASH bus failing `FDCAN_RuntimeBringUp` → `Error_Handler()`
→ the whole ECU silent, including the AMS heartbeat. The rewrite drops FDCAN3 and
brings FDCAN2 (ACU) up independently of FDCAN1 (INV). These assert that:
  - 0x100 streams on FDCAN2/can2 with the INV bus unpeered (B-001)
  - an unpeered/degraded FDCAN1 does NOT silence FDCAN2 TX (B-002)
  - sustained traffic on both buses doesn't corrupt (non-overlapping MessageRAM,
    FDCAN1=0 / FDCAN2=387 words) (B-003)
  - TX is alive from the first control cycle, not gated behind a peer/FSM (B-004)
"""
from __future__ import annotations

import time

from tools.firmware_test.vcu import can_map as M


def _hb_stamps(observe, ext, window_s):
    observe.clear()
    stamps, last = [], None
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        f = observe.last(M.ID_HEARTBEAT, extended=ext)
        if f is not None and f.timestamp != last:
            stamps.append(f.timestamp)
            last = f.timestamp
        time.sleep(0.002)
    return stamps


class TestB001Fdcan2Independent:

    def test_b001_heartbeat_with_inv_bus_unpeered(self, fresh_boot, observe_acu,
                                                  vcu_profile):
        """B-001: 0x100 streams on FDCAN2/can2 with the FDCAN1/INV bus unpeered
        (no inverter sim running) — FDCAN2 comes up independently of FDCAN1."""
        ext = bool(vcu_profile.get("heartbeat_extended", False))
        stamps = _hb_stamps(observe_acu, ext, 2.0)
        assert len(stamps) >= 40, \
            f"only {len(stamps)} 0x100 in 2 s with INV unpeered (FDCAN2 not independent?)"


class TestB002Fdcan1FailDoesntSilenceTx:

    def test_b002_inv_degraded_keeps_acu_tx(self, fresh_boot, observe_acu,
                                            vcu_profile):
        """B-002: the FDCAN1/INV bus has no peer (the ECU's 0x360 TX gets no ACK
        → error-passive), yet FDCAN2 TX is NOT silenced — 0x100 keeps streaming
        with bounded gaps. The #48 whole-ECU-Error_Handler guard."""
        ext = bool(vcu_profile.get("heartbeat_extended", False))
        max_gap = float(vcu_profile["heartbeat_max_gap_ms"]) / 1000.0 * 4
        stamps = _hb_stamps(observe_acu, ext, 3.0)
        assert len(stamps) >= 60, \
            f"0x100 fell off under unpeered INV bus: {len(stamps)} frames in 3 s"
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        worst = max(gaps) if gaps else 99.0
        assert worst <= max_gap, \
            f"0x100 gap {worst*1000:.0f} ms > {max_gap*1000:.0f} ms (INV bus disrupted TX)"


class TestB003NonOverlappingRam:

    def test_b003_both_buses_under_traffic(self, fresh_boot, inv_heartbeat,
                                           observe_acu, vcu_profile):
        """B-003: sustained inverter RX on FDCAN1 + 0x100 TX on FDCAN2 at once —
        neither corrupts. Non-overlapping MessageRAM (FDCAN1=0 / FDCAN2=387
        words): 0x100 keeps streaming and decodes sanely while RX runs."""
        ext = bool(vcu_profile.get("heartbeat_extended", False))
        inv_heartbeat["vdc_ready"](400)     # 0x466 + 0x461 streaming on can0
        time.sleep(0.5)
        stamps = _hb_stamps(observe_acu, ext, 2.0)
        assert len(stamps) >= 40, \
            f"0x100 corrupted under dual-bus traffic: {len(stamps)} frames in 2 s"
        f = observe_acu.last(M.ID_HEARTBEAT, extended=ext)
        assert f is not None and 0 <= M.decode_heartbeat(f.data)["dc_bus_v"] <= 1000, \
            "0x100 payload garbled under dual-bus traffic (RAM bleed?)"


class TestB004TxFromFirstCycle:

    def test_b004_tx_alive_before_any_peer(self, fresh_boot, pit_diag, observe_acu,
                                           vcu_profile):
        """B-004: TX is alive from the first control cycle — 0x100 appears soon
        after boot with NO peer and the FSM still in WaitInvVdcConfig, i.e. TX is
        not gated behind FSM progress or a received frame."""
        dt = fresh_boot["t_first_frame"] - fresh_boot["t_power_on"]
        assert dt < 6.0, f"0x100 took {dt:.1f} s after power-on — TX gated?"
        fsm = pit_diag["read_fsm"]()
        assert fsm == int(M.VcuFsmState.WAIT_INV_VDC_CONFIG), \
            f"FSM at {M.VcuFsmState.name_of(fsm)}, not the boot state — TX shouldn't need progress"
