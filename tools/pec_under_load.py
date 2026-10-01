#!/usr/bin/env python3
"""PEC error rate with the emulator's CDC idle vs hammered.

Stalled MISO shows up on the AMS as a PEC failure, not as bad telemetry
(the AMS rejects and retries). So PEC counters -- not cell values -- are
the honest measure of what a TX stall costs. Reads the pit-diag
aggregate 0x6C0[4..5] and the per-IC pair 0x6C7/0x6C8.

Firmware-independent: run it on 0.6.0 and 0.7.0 to compare directly.
"""
import subprocess, sys, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.pico_ltc_emulator.host.pico_ltc_client import PicoLtcClient
from tools.firmware_test.ams import can_map as M

BUS, SECS = "can2", 60
SLOT_RELAY_BIT, INA = 1, 0x41          # MLC2


def power_cycle():
    """The pit-diag PEC aggregate is a saturating u16 and only clears on
    an AMS boot, so every run has to start from a cold carrier or the
    comparison is against a pegged 65535."""
    from broker.server import BrokerClient
    c = BrokerClient("/run/hil-broker/broker.sock")
    c.call("tca.set_direction", addr=0x20, port=0, mask=0x00)
    c.call("tca.write_pin", addr=0x20, port=0, pin=SLOT_RELAY_BIT, value=False)
    time.sleep(4.0)
    c.call("tca.write_pin", addr=0x20, port=0, pin=SLOT_RELAY_BIT, value=True)
    time.sleep(0.5)
    mA = c.call("ina.current", addr=INA) * 1000
    c.close()
    return mA


def cansend(payload):
    subprocess.run(["cansend", BUS, "%03X#%s" % (M.ID_PIT_DIAG_CMD, payload.hex().upper())],
                   check=False, capture_output=True, timeout=2)


def pec(secs=3.0):
    """(aggregate, per-IC list) from one pit-diag scan."""
    out = subprocess.run(["timeout", str(secs), "candump", BUS],
                         capture_output=True, text=True).stdout
    agg, per = None, None
    for ln in out.splitlines():
        f = ln.split()
        if len(f) < 4:
            continue
        b = f[3:]
        if f[1] == "6C0" and len(b) == 8:
            agg = int(b[4], 16) << 8 | int(b[5], 16)
        elif f[1] == "6C7" and len(b) == 8:
            per = [int(x, 16) for x in b]
    return agg, per


with PicoLtcClient() as p:
    ver = p.ping()
    p.set_all_cells(3750)
    p.set_all_temps(250)

print("pico:", ver)
print("cold-booting MLC2 to zero the PEC aggregate ...")
print("  draw = %.1f mA" % power_cycle())
time.sleep(6.0)

cansend(M.PIT_DIAG_ENABLE_MAGIC)
time.sleep(2.0)

a0, p0 = pec()
if a0 is None:
    print("no 0x6C0 seen -- pit-diag did not arm"); sys.exit(1)
if a0 >= 0xFFFF:
    print("pec_err already saturated (%d) -- measurement would be meaningless" % a0)
    sys.exit(1)
print("armed, pec_err = %d" % a0)

print("\n[idle CDC] %d s ..." % SECS)
time.sleep(SECS)
a1, p1 = pec()
print("  pec_err +%d" % (a1 - a0,))

print("\n[busy CDC] %d s ..." % SECS)
with PicoLtcClient() as p:
    stop, res = threading.Event(), {}

    def hammer():
        n = 0
        while not stop.is_set():
            try:
                p.status(); n += 1
            except Exception:
                break
        res["n"] = n

    th = threading.Thread(target=hammer, daemon=True); th.start()
    time.sleep(SECS)
    a2, p2 = pec()
    stop.set(); th.join(timeout=5)
    try:
        stall = p.status().get("tx_stall_cmd", "n/a")
    except Exception:
        stall = "n/a"
print("  pec_err +%d   (%d polls, tx_stall_cmd now %s)"
      % (a2 - a1, res.get("n", 0), stall))

cansend(M.PIT_DIAG_DISABLE_MAGIC)
print("\nSUMMARY %s: idle +%d, busy +%d PEC errors over %d s each"
      % (ver.split()[-1], a1 - a0, a2 - a1, SECS))
