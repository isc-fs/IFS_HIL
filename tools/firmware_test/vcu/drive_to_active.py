#!/usr/bin/env python3
"""Drive the ECU startup FSM from boot to ACTIVE in one command (HIL bench).

Composes the two CAN sims + the broker-driven pedals/button to walk the ECU FSM
(IFS08-CE-ECU Core/Src/control.c) through its whole startup spine, watching
pit-diag 0x700[0] (= fsm_state) at each gate:

  WAIT_INV_VDC_CONFIG --(inverter Vdc + fresh AMS)--> BOOT --(AMS ok_precarga)-->
  WAIT_START_BRAKE --(start btn + brake)--> R2D_DELAY --(2 s)--> WAIT_INV_STANDBY
  --(inv_state==3)--> ACTIVE --(inv_state==6 + APPS)--> torque

The AMS is a PLUGGABLE source so the bench can move off the sim later:
  --ams sim   (default) -- this script streams 0x4A0(Run)+0x020 on the ACU bus.
  --ams real            -- power MLC2 (the real AMS) and let IT publish 0x4A0/0x020;
                           this script injects NO AMS frames. The real AMS must
                           independently reach Run + precharge-complete (its own
                           TSMS/DASH_CHG/BMS bring-up) -- we only power it and
                           verify 0x4A0 is arriving.

NB observation needs the ECU #48 transmit fix: the stimulus drives the FSM either
way, but 0x700/0x100 only come back once FDCAN TX is unblocked. Until then this
runs the sequence "blind" and reports fsm=None at each gate.

Run on the Pi:  python3 drive_to_active.py [--ams sim|real]
"""
import argparse
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/home/isc/IFS08_HIL")
from broker.server import BrokerClient                     # noqa: E402
from tools.firmware_test.vcu import inverter_sim as INV    # noqa: E402
from tools.firmware_test.vcu import ams_sim as AMS         # noqa: E402
import can                                                 # noqa: E402

FSM = {0: "WAIT_INV_VDC_CONFIG", 1: "BOOT", 2: "WAIT_PRECHARGE_ACK",
       3: "WAIT_START_BRAKE", 4: "R2D_DELAY", 5: "WAIT_INV_STANDBY",
       6: "ACTIVE", 7: "AMS_ERROR"}
ID_PIT_STATUS = 0x700
ID_PIT_ENABLE = 0x7E0
ID_AMS_STATUS = 0x4A0
# MLC4 = ECU (K4 = TCA 0x20 p3); MLC2 = AMS (K2 = TCA 0x20 p1)
ECU_RELAY_PIN, AMS_RELAY_PIN = 3, 1


def ip(ch, sp):
    for cmd in (["down"], ["type", "can", "bitrate", "500000", "sample-point", sp, "restart-ms", "200"],
                ["txqueuelen", "1000"], ["up"]):
        subprocess.run(["sudo", "ip", "link", "set", ch] + cmd, check=False)


def apps_volts(profile_min, profile_max, pct):
    raw = profile_min + pct / 100.0 * (profile_max - profile_min)
    return max(0.0, min(3.3, raw / 4095.0 * 3.3))


def main():
    ap = argparse.ArgumentParser(description="drive the ECU FSM to ACTIVE")
    ap.add_argument("--ams", choices=["sim", "real"], default="sim")
    ap.add_argument("--inv", default="can0", help="INV bus (FDCAN1)")
    ap.add_argument("--acu", default="can2", help="ACU bus (FDCAN2)")
    ap.add_argument("--gate-timeout", type=float, default=6.0)
    args = ap.parse_args()

    ip(args.inv, "0.875")   # FDCAN1/INV now 87.5% (ECU #57)
    ip(args.acu, "0.688")   # FDCAN2/ACU 68.75%

    c = BrokerClient("/run/hil-broker/broker.sock")
    c.call("tca.set_direction", addr=0x20, port=0, mask=0x00)
    if args.ams == "real":
        c.call("tca.write_pin", addr=0x20, port=0, pin=AMS_RELAY_PIN, value=True)
        print("MLC2 (real AMS) powered -- it must independently reach Run/precharge")
    # reboot the ECU
    c.call("tca.write_pin", addr=0x20, port=0, pin=ECU_RELAY_PIN, value=False)
    time.sleep(1.5)
    c.call("tca.write_pin", addr=0x20, port=0, pin=ECU_RELAY_PIN, value=True)

    inv_bus = can.interface.Bus(args.inv, interface="socketcan")
    acu_bus = can.interface.Bus(args.acu, interface="socketcan")
    rx_bus = can.interface.Bus(args.acu, interface="socketcan")

    st = {"inv": 0, "vdc": 0, "sendvdc": False, "ams": 3, "sess": 1, "prech": 1,
          "cnt": 0, "run": True, "sim": args.ams == "sim", "fsm": None, "ams_seen": False}

    def sender():
        while st["run"]:
            st["cnt"] = (st["cnt"] + 1) & 0x0F
            inv_bus.send(can.Message(arbitration_id=0x461, is_extended_id=False, data=INV.f_461(st["inv"], 0, st["cnt"])))
            if st["sendvdc"]:
                inv_bus.send(can.Message(arbitration_id=0x466, is_extended_id=False, data=INV.f_466(st["vdc"], st["cnt"])))
            if st["sim"]:
                acu_bus.send(can.Message(arbitration_id=ID_AMS_STATUS, is_extended_id=False, data=AMS.f_4a0(st["ams"], st["sess"])))
                acu_bus.send(can.Message(arbitration_id=0x020, is_extended_id=False, data=AMS.f_020(st["prech"])))
            time.sleep(0.02)

    def receiver():
        while st["run"]:
            m = rx_bus.recv(timeout=0.4)
            if m is None:
                continue
            if m.arbitration_id == ID_PIT_STATUS and len(m.data) >= 1:
                st["fsm"] = m.data[0]
            elif m.arbitration_id == ID_AMS_STATUS:
                st["ams_seen"] = True

    threading.Thread(target=sender, daemon=True).start()
    threading.Thread(target=receiver, daemon=True).start()

    def wait_fsm(target):
        deadline = time.time() + args.gate_timeout
        while time.time() < deadline:
            if st["fsm"] == target:
                return True
            time.sleep(0.1)
        return False

    def show():
        return FSM.get(st["fsm"], "None (no 0x700 -- #48 TX still blocked?)")

    time.sleep(3.0)  # let the app boot
    if args.ams == "real" and not st["ams_seen"]:
        print("WARNING: --ams real but no 0x4A0 seen -- the MLC2 AMS isn't publishing "
              "(needs its own bring-up to Run). The FSM will stall at precharge.")
    acu_bus.send(can.Message(arbitration_id=ID_PIT_ENABLE, is_extended_id=False, data=bytes.fromhex("DEADBEEF")))
    time.sleep(0.5)
    print("[0] boot + pit-diag enabled -> fsm=%s" % show())

    st["vdc"], st["sendvdc"] = 400, True
    print("[1] inverter Vdc on  -> %s" % ("WAIT_START_BRAKE" if wait_fsm(3) else "no advance, fsm=%s" % show()))

    c.call("tca.set_direction", addr=0x22, port=1, mask=0x00)
    c.call("tca.write_pin", addr=0x22, port=1, pin=0, value=True)     # start button
    c.call("dac.set_voltage", idx=0, channel=0, volts=1.5)            # brake > arm threshold
    print("[2] start btn + brake -> %s" % ("R2D_DELAY" if wait_fsm(4) else "no advance, fsm=%s" % show()))

    wait_fsm(5)
    st["inv"] = 3                                                     # inverter -> standby
    print("[3] inverter standby -> %s" % ("ACTIVE" if wait_fsm(6) else "fsm=%s" % show()))

    st["inv"] = 6                                                     # inverter -> torque
    c.call("dac.set_voltage", idx=0, channel=1, volts=apps_volts(2050, 2950, 50))  # APPS1 ~50%
    c.call("dac.set_voltage", idx=0, channel=2, volts=apps_volts(1915, 2570, 50))  # APPS2 ~50%
    time.sleep(0.6)
    print("[4] torque commanded (inv=6, APPS~50%%) -> fsm=%s" % show())

    print("=== final fsm = %s ===" % show())
    st["run"] = False
    time.sleep(0.1)
    for b in (inv_bus, acu_bus, rx_bus):
        try:
            b.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
