#!/usr/bin/env python3
"""Cold-boot the AMS carrier against the emulator.

The 0.7.0 change drops the 4 pre-loaded chip-0 data bytes and makes the
CPU supply every data byte after a byte-2 opcode parse. The pre-load
existed so the very first xact -- the AMS boot-discovery RDCFGA --
could never miss its PEC, because a PEC failure there latches ERROR in
BKP RAM for the whole session. This power-cycles MLC2 and checks the
AMS comes up clean against the new firmware.
"""
import subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from broker.server import BrokerClient
from tools.firmware_test.ams.can_map import decode_telem_status
from tools.pico_ltc_emulator.host.pico_ltc_client import PicoLtcClient

SLOT, RELAY_BIT, INA = 2, 1, 0x41

with PicoLtcClient() as p:
    print("pico:", p.ping())
    p.set_all_cells(3750)
    p.set_all_temps(250)
    before = p.status()

c = BrokerClient("/run/hil-broker/broker.sock")
c.call("tca.set_direction", addr=0x20, port=0, mask=0x00)

print("\n[1/3] de-energising MLC%d ..." % SLOT)
c.call("tca.write_pin", addr=0x20, port=0, pin=RELAY_BIT, value=False)
time.sleep(4.0)

print("[2/3] energising MLC%d ..." % SLOT)
c.call("tca.write_pin", addr=0x20, port=0, pin=RELAY_BIT, value=True)
time.sleep(0.5)
print("      draw = %.1f mA" % (c.call("ina.current", addr=INA) * 1000))

print("[3/3] watching can2 for first telemetry (12 s) ...")
out = subprocess.run(["timeout", "12", "candump", "can2"],
                     capture_output=True, text=True).stdout

first, last = None, None
for ln in out.splitlines():
    f = ln.split()
    if len(f) >= 4 and f[1] == "4A0":
        d = bytes(int(x, 16) for x in f[3:])
        if len(d) == 8:
            dec = decode_telem_status(d)
            if first is None:
                first = dec
            last = dec

def show(tag, d):
    if d is None:
        print("  %-8s <no 0x4A0 seen>" % tag)
        return
    print("  %-8s state=%-10s ams_ok=%-5s min=%-5s max=%-5s mask=0x%02X"
          % (tag, d.get("state"), d.get("ams_ok"), d.get("min_cell_mV"),
             d.get("max_cell_mV"), d.get("module_online_mask", 0)))

print()
show("first", first)
show("last", last)

with PicoLtcClient() as p:
    after = p.status()
print("\ntx_stall_cmd = %s (was %s)   n_cs_cycles = %s"
      % (after.get("tx_stall_cmd"), before.get("tx_stall_cmd"), after.get("n_cs_cycles")))

ok = (last is not None and last.get("min_cell_mV") == 3750
      and last.get("max_cell_mV") == 3750
      and int(after.get("tx_stall_cmd", 1)) == 0)
print("RESULT:", "PASS - cold boot clean against 0.7.0" if ok else "FAIL - inspect above")
