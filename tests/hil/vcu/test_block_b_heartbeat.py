"""
Block B — VCU heartbeat & liveness (IFS08-CE-ECU#35).

Guards the headline regression: the VCU must stream `0x100` in ALL states,
never stopping across FSM transitions (the VcuStale fix — the ECU only sent
0x100 during precharge, tripping the AMS). SIL ref: --test-full-cycle.

SCOPE: B-001's full "streams in every state" and B-002 "survives precharge"
need the FSM-transition stimulus (precharge 0x020 / inverter 0x466), which
lands with the C-block fixtures — marked TODO. Here: boot-time presence +
cadence + decode.
"""
from __future__ import annotations

import time

from tools.firmware_test.vcu import can_map as M


def _collect_heartbeats(observe, ext, window_s):
    """Poll the observer for `window_s`; return kernel timestamps of distinct
    0x100 frames."""
    observe.clear()
    stamps = []
    last_ts = None
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        f = observe.last(M.ID_HEARTBEAT, extended=ext)
        if f is not None and f.timestamp != last_ts:
            stamps.append(f.timestamp)
            last_ts = f.timestamp
        time.sleep(0.002)
    return stamps


class TestB001HeartbeatStreams:

    def test_b001_heartbeat_present_and_cadenced(self, fresh_boot, observe_acu,
                                                 vcu_profile):
        """B-001 (boot scope): 0x100 present right after boot, no gap > 3x the
        nominal period. Cross-transition continuity TODO (needs C-block stim)."""
        assert fresh_boot["first_frame"]["dc_bus_v"] is not None
        ext = bool(vcu_profile.get("heartbeat_extended", False))
        max_gap_s = float(vcu_profile["heartbeat_max_gap_ms"]) / 1000.0 * 3
        stamps = _collect_heartbeats(observe_acu, ext, 1.5)
        assert len(stamps) >= 5, \
            f"only {len(stamps)} 0x100 frames in 1.5 s -- heartbeat not streaming"
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        worst = max(gaps) if gaps else 0.0
        assert worst <= max_gap_s, \
            f"max 0x100 gap {worst*1000:.0f} ms > {max_gap_s*1000:.0f} ms allowed"


class TestB003DcBusValue:

    def test_b003_dc_bus_decodes_sane(self, fresh_boot):
        """B-003 (partial): 0x100 decodes as a plausible LE u16 dc_bus value.
        Full scaling cross-check vs ecu.dbc / the AMS 0x100 decode TODO."""
        dc = fresh_boot["first_frame"]["dc_bus_v"]
        assert 0 <= dc <= 1000, f"dc_bus_v {dc} V out of plausible 0..1000 range"
