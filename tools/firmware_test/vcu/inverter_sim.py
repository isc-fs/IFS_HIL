#!/usr/bin/env python3
"""Standalone inverter (NX0001-STS04, node EMC) simulator for the ECU/VCU HIL bench.

Streams the inverter's TX frames on the INV bus (FDCAN1 = kernel **can0**) so the
ECU's startup FSM advances: 0x466 sets `inv_vdc_ready` (leaves WAIT_INV_VDC_CONFIG),
0x461 carries `inv_state` (WAIT_INV_STANDBY -> ACTIVE on state==3; 10/11 = fault).

The CONTRACT is the ECU firmware's RX parser (IFS08-CE-ECU Core/Src/can.c, the
CAN_BUS_INV switch), NOT the DBC signal names. Byte layouts (verified vs can.c):
  0x461 TX_STATE_2 (7B): [4]=inv_state & 0x0F ; [2]=inv_error (read only if state 10/11)
  0x463 TX_STATE_4 (8B): [5..7]=rpm, 20-bit signed (lo,mid,hi-nibble)
  0x464 TX_STATE_5 (8B): [0]=motor_temp [1]=igbt_temp [2]=air_temp  (NB byte0 is a TEMP,
                         so this frame has no E2E CRC at byte0 -- firmware/DBC mismatch worth a look)
  0x465 TX_STATE_6 (8B): [2:4]=speed LE ; [4:6]=current LE
  0x466 TX_STATE_7 (6B): [2:4]=dc_bus LE  <- sets inv_vdc_ready
The ECU does NOT validate AUTOSAR E2E-P1 on RX, so byte0(CRC)/byte1(CNT) are left
as a rolling counter for realism (no CRC computed) -- except 0x464 (see above).

Usage on the Pi (INV bus = can0, sample-point 37.5% to match the ECU's FDCAN1):
  sudo ip link set can0 down
  sudo ip link set can0 type can bitrate 500000 sample-point 0.375 restart-ms 200
  sudo ip link set can0 txqueuelen 1000 ; sudo ip link set can0 up
  python3 inverter_sim.py --channel can0 --state 3 --vdc 400

  python3 inverter_sim.py --selftest      # offline: print + assert the byte layout
"""
import argparse
import time


def f_461(state, err, cnt):
    b = bytearray(7)
    b[1] = cnt & 0x0F
    b[2] = err & 0xFF
    b[4] = state & 0x0F
    return bytes(b)


def f_463(rpm, cnt):
    r = rpm & 0xFFFFF
    b = bytearray(8)
    b[1] = cnt & 0x0F
    b[5] = r & 0xFF
    b[6] = (r >> 8) & 0xFF
    b[7] = (r >> 16) & 0x0F
    return bytes(b)


def f_464(motor_c, igbt_c, air_c):
    b = bytearray(8)
    b[0] = motor_c & 0xFF
    b[1] = igbt_c & 0xFF
    b[2] = air_c & 0xFF
    return bytes(b)


def f_465(speed, current, cnt):
    b = bytearray(8)
    b[1] = cnt & 0x0F
    b[2] = speed & 0xFF
    b[3] = (speed >> 8) & 0xFF
    b[4] = current & 0xFF
    b[5] = (current >> 8) & 0xFF
    return bytes(b)


def f_466(vdc, cnt):
    b = bytearray(6)
    b[1] = cnt & 0x0F
    b[2] = vdc & 0xFF
    b[3] = (vdc >> 8) & 0xFF
    return bytes(b)


def build_round(args, cnt):
    """The 5 frames the ECU reads, in (id, dlc, data) form."""
    return [
        (0x461, 7, f_461(args.state, args.err, cnt)),
        (0x466, 6, f_466(args.vdc, cnt)),
        (0x463, 8, f_463(args.rpm, cnt)),
        (0x464, 8, f_464(args.motor_temp, args.igbt_temp, args.air_temp)),
        (0x465, 8, f_465(args.speed, args.current, cnt)),
    ]


def selftest():
    class A:
        state, err, vdc, rpm = 3, 0, 400, 1500
        motor_temp, igbt_temp, air_temp = 30, 40, 25
        speed, current = 1500, 120
    rnd = {i: d for (i, _, d) in build_round(A, 5)}
    assert rnd[0x461][4] & 0x0F == 3, "0x461 byte4 must be inv_state"
    assert int.from_bytes(rnd[0x466][2:4], "little") == 400, "0x466 bytes2-3 = dc_bus LE"
    assert rnd[0x464][0] == 30 and rnd[0x464][1] == 40 and rnd[0x464][2] == 25, "0x464 temps"
    assert int.from_bytes(rnd[0x465][2:4], "little") == 1500, "0x465 speed LE"
    assert int.from_bytes(rnd[0x465][4:6], "little") == 120, "0x465 current LE"
    rpm = rnd[0x463][5] | (rnd[0x463][6] << 8) | ((rnd[0x463][7] & 0x0F) << 16)
    assert rpm == 1500, "0x463 rpm bytes5-7"
    for i, dlc, d in build_round(A, 5):
        print("0x%03X [%d] %s" % (i, dlc, d.hex(" ")))
    print("selftest OK -- byte layout matches the ECU firmware RX parser")


def main():
    ap = argparse.ArgumentParser(description="ECU HIL inverter (EMC) simulator")
    ap.add_argument("--channel", default="can0", help="INV bus = FDCAN1 = can0")
    ap.add_argument("--state", type=int, default=3,
                    help="inv_state: 3=STANDBY(->ACTIVE) 4=READY 6=TORQUE 10/11=fault")
    ap.add_argument("--err", type=int, default=0, help="inv_error byte (used when state 10/11)")
    ap.add_argument("--vdc", type=int, default=400, help="DC-bus volts")
    ap.add_argument("--rpm", type=int, default=0)
    ap.add_argument("--speed", type=int, default=0)
    ap.add_argument("--current", type=int, default=0)
    ap.add_argument("--motor-temp", type=int, default=30, dest="motor_temp")
    ap.add_argument("--igbt-temp", type=int, default=40, dest="igbt_temp")
    ap.add_argument("--air-temp", type=int, default=25, dest="air_temp")
    ap.add_argument("--period", type=float, default=0.02, help="heartbeat period s (default 20ms)")
    ap.add_argument("--selftest", action="store_true", help="offline byte-layout check, no CAN")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    import can
    bus = can.interface.Bus(channel=args.channel, interface="socketcan")
    print("inverter-sim -> %s: state=%d vdc=%dV rpm=%d @ %.0fms (Ctrl-C to stop)"
          % (args.channel, args.state, args.vdc, args.rpm, args.period * 1000))
    cnt = 0
    try:
        while True:
            cnt = (cnt + 1) & 0x0F
            for fid, _dlc, data in build_round(args, cnt):
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
