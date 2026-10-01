#!/usr/bin/env python3
"""Standalone AMS/ACU simulator for the ECU/VCU HIL bench.

Streams the two AMS frames the ECU's startup FSM gates on, so the FSM can leave
the precharge wait and advance toward ACTIVE (Core/Src/control.c). With no live
AMS the FSM is stuck at BOOT / WAIT_PRECHARGE_ACK forever, and the AMS-authority
guards keep de-arming it. Pair this with inverter_sim.py (the INV side).

Contract = the ECU RX parser (Core/Src/can.c) + app_state.h:
  0x4A0 AMS_STATUS (8B): [0]=ams_state (0=Start, 3=Run, 5=Error), [3]=session_id.
        Must stream >=1 Hz (ams_status_stale window = 1 s). NB: state Start/Error
        also CLEARS ok_precarga in the ECU (can.c:164), so the happy path needs
        state = Run (3).
  0x020 (8B): [0]!=0 -> ok_precarga = 1 (precharge complete). Must stream >=1 Hz
        too -- precharge_complete() requires it NOT stale.
Both on the ACU bus = FDCAN2 = kernel can2 @ 68.75%.

Usage on the Pi (set can2 to the ACU sample-point first, then):
  python3 ams_sim.py --channel can2 --state 3 --session 1 --precharge 1   # happy path
  python3 ams_sim.py --state 5      # AMS Error  (inhibit  -> Block E / AMS_ERROR)
  python3 ams_sim.py --state 0      # AMS Start  (de-arm   -> Block J)
  python3 ams_sim.py --session 7    # change session-id mid-run (de-arm -> J-003)
  python3 ams_sim.py --selftest     # offline byte-layout check
"""
import argparse
import time

ID_AMS_STATUS = 0x4A0   # AMS FSM state + session id
ID_PRECHARGE  = 0x020   # ok_precarga ack


def f_4a0(state, session):
    b = bytearray(8)
    b[0] = state & 0xFF       # ams_state: 0=Start 3=Run 5=Error
    b[3] = session & 0xFF     # ams_session_id
    return bytes(b)


def f_020(ok):
    b = bytearray(8)
    b[0] = 1 if ok else 0     # ok_precarga
    return bytes(b)


def build_round(args):
    return [(ID_AMS_STATUS, f_4a0(args.state, args.session)),
            (ID_PRECHARGE,  f_020(args.precharge))]


def selftest():
    class A:
        state, session, precharge = 3, 1, 1
    rnd = {i: d for (i, d) in build_round(A)}
    assert rnd[0x4A0][0] == 3, "0x4A0 byte0 must be ams_state"
    assert rnd[0x4A0][3] == 1, "0x4A0 byte3 must be session_id"
    assert rnd[0x020][0] == 1, "0x020 byte0 must be ok_precarga"
    for i, d in build_round(A):
        print("0x%03X [8] %s" % (i, d.hex(" ")))
    print("selftest OK -- matches can.c (0x4A0[0]=state, [3]=session; 0x020[0]=ok_precarga)")


def main():
    ap = argparse.ArgumentParser(description="ECU HIL AMS/ACU simulator")
    ap.add_argument("--channel", default="can2", help="ACU bus = FDCAN2 = can2")
    ap.add_argument("--state", type=int, default=3, help="ams_state: 0=Start 3=Run 5=Error")
    ap.add_argument("--session", type=int, default=1, help="ams_session_id (0x4A0[3])")
    ap.add_argument("--precharge", type=int, default=1, help="0x020 ok_precarga (0/1)")
    ap.add_argument("--period", type=float, default=0.05, help="stream period s (default 50ms, well inside the 1 s staleness)")
    ap.add_argument("--selftest", action="store_true", help="offline byte-layout check, no CAN")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    import can
    bus = can.interface.Bus(channel=args.channel, interface="socketcan")
    print("ams-sim -> %s: state=%d session=%d precharge=%d @ %.0fms (Ctrl-C to stop)"
          % (args.channel, args.state, args.session, args.precharge, args.period * 1000))
    try:
        while True:
            for fid, data in build_round(args):
                bus.send(can.Message(arbitration_id=fid, is_extended_id=False, data=data))
            time.sleep(args.period)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        try:
            bus.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
