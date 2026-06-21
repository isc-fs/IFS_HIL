"""
Block H — VCU pit-diag stream (IFS08-CE-ECU#71).

The pit-diag stream is the bench's only observability into the ECU (no UART/SWD
on the car). Gated by 0x7E0='DEADBEEF' (ack 0x7E1): 0x700-0x703 + 0x705 @100 ms,
0x704 @1 Hz. Every payload must decode against ecu.dbc (mirrored in can_map).
"""
from __future__ import annotations

import time

from tools.firmware_test.acu_stim import AcuStim
from tools.firmware_test.vcu import can_map as M

_FAST_IDS = [
    (M.ID_PIT_STATUS, "0x700"), (M.ID_PIT_PEDALS, "0x701"),
    (M.ID_PIT_INVERTER, "0x702"), (M.ID_PIT_FWINFO, "0x703"),
    (M.ID_PIT_BRAKE, "0x705"),
]


def _enable(stim):
    stim.send_raw(M.ID_PIT_ENABLE, bytes.fromhex("DEADBEEF"), is_extended_id=False)


def _disable(stim):
    stim.send_raw(M.ID_PIT_ENABLE, bytes([0, 0, 0, 0]), is_extended_id=False)


def _count(observe, can_id, window_s):
    """Count distinct frames of `can_id` over `window_s` (poll-last + dedupe)."""
    observe.clear()
    seen, last = 0, None
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        f = observe.last(can_id, extended=False)
        if f is not None and f.timestamp != last:
            seen += 1
            last = f.timestamp
        time.sleep(0.004)
    return seen


class TestH001Enable:

    def test_h001_enable_starts_stream(self, fresh_boot, observe_acu, vcu_profile):
        """H-001: 0x7E0=DEADBEEF starts 0x700-0x703+0x705 @100 ms, 0x704 @1 Hz,
        and the ack 0x7E1 reads 1 within one cycle."""
        stim = AcuStim(channel=vcu_profile["pit_diag_bus"])
        stim.start()
        try:
            _enable(stim)
            time.sleep(0.4)
            # 0x7E1 is a one-shot reply to 0x7E0 (not streamed) -- check it FIRST,
            # before the _count() loop below clears the observer.
            ack = observe_acu.last(M.ID_PIT_ACK, extended=False)
            assert ack is not None and ack.data[0] == 1, \
                "0x7E1 ack != 1 after enable"
            for cid, name in _FAST_IDS:
                n = _count(observe_acu, cid, 1.0)
                assert n >= 5, f"{name} only {n} frames in 1 s (want >=5 @100 ms)"
            assert _count(observe_acu, M.ID_PIT_HEALTH, 1.6) >= 1, \
                "0x704 health absent after enable"
        finally:
            _disable(stim)
            stim.stop()


class TestH002Disable:

    def test_h002_disable_stops_stream(self, fresh_boot, observe_acu, vcu_profile):
        """H-002: 0x7E0=0 stops the stream and 0x7E1 ack -> 0."""
        stim = AcuStim(channel=vcu_profile["pit_diag_bus"])
        stim.start()
        try:
            _enable(stim)
            time.sleep(0.4)
            assert _count(observe_acu, M.ID_PIT_STATUS, 0.6) >= 2, \
                "stream didn't start, can't test disable"
            observe_acu.clear()           # drop the stale enable-ack
            _disable(stim)
            time.sleep(0.5)
            # ack first (one-shot), before the _count() clear below
            ack = observe_acu.last(M.ID_PIT_ACK, extended=False)
            assert ack is not None and ack.data[0] == 0, \
                "0x7E1 ack != 0 after disable"
            n = _count(observe_acu, M.ID_PIT_STATUS, 0.8)
            assert n == 0, f"0x700 still streaming ({n} frames) after disable"
        finally:
            stim.stop()


class TestH003Decode:

    def test_h003_all_payloads_decode(self, fresh_boot, observe_acu, vcu_profile):
        """H-003: every pit-diag payload decodes via can_map and yields plausible
        fields (fwinfo git non-zero, fsm_state a known enum)."""
        stim = AcuStim(channel=vcu_profile["pit_diag_bus"])
        stim.start()
        try:
            _enable(stim)
            time.sleep(0.6)
            decoders = {
                M.ID_PIT_STATUS:   M.decode_pit_status,
                M.ID_PIT_PEDALS:   M.decode_pit_pedals,
                M.ID_PIT_INVERTER: M.decode_pit_inverter,
                M.ID_PIT_FWINFO:   M.decode_pit_fwinfo,
                M.ID_PIT_HEALTH:   M.decode_pit_health,
                M.ID_PIT_BRAKE:    M.decode_pit_brake,
            }
            for cid, dec in decoders.items():
                f = observe_acu.last(cid, extended=False)
                assert f is not None, f"0x{cid:03X} not seen on the stream"
                d = dec(f.data)
                assert isinstance(d, dict) and d, f"0x{cid:03X} decoded empty"
            fw = M.decode_pit_fwinfo(
                observe_acu.last(M.ID_PIT_FWINFO, extended=False).data)
            assert fw["git_hash"] != 0, "fwinfo git_hash zero"
            st = M.decode_pit_status(
                observe_acu.last(M.ID_PIT_STATUS, extended=False).data)
            assert st["fsm_state"] in {s.value for s in M.VcuFsmState}, \
                f"fsm_state {st['fsm_state']} not a known VcuFsmState"
        finally:
            _disable(stim)
            stim.stop()
