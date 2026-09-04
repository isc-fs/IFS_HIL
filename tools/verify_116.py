#!/usr/bin/env python3
"""Stronger IFS_HIL#116 verification.

`measure_116` holds every cell identical, so a response that were still
uniform-but-wrong would pass. Here module 0 cell 0 is driven to values
distinct from the rest of the pack, and min/max telemetry must follow it.
Also checks the temperature path still decodes (0x136) so the byte-2
parse didn't trade cell correctness for AUX correctness.
"""
import subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.pico_ltc_emulator.host.pico_ltc_client import PicoLtcClient

BASE = 3750


def sample(iface="can2", secs=2.0):
    out = subprocess.run(["timeout", str(secs), "candump", iface],
                         capture_output=True, text=True).stdout
    got = {}
    for ln in out.splitlines():
        f = ln.split()
        if len(f) >= 4:
            got[f[1]] = f[3:]

    def u16(b, i):
        return int(b[i], 16) << 8 | int(b[i + 1], 16)

    a0 = got.get("4A0")
    mn = u16(a0, 4) if a0 and len(a0) >= 8 else None
    mx = u16(a0, 6) if a0 and len(a0) >= 8 else None
    t = got.get("136")
    temps = [int(t[i + 1], 16) for i in (0, 2, 4)] if t and len(t) >= 6 else []
    return mn, mx, temps


fails = []
with PicoLtcClient() as p:
    print("pico:", p.ping())
    p.set_all_cells(BASE)
    p.set_all_temps(250)
    time.sleep(2.5)

    print("\n-- module 0 cell 0 driven distinct, rest at %d mV --" % BASE)
    print("%12s | %6s %6s | %s" % ("m0c0 set", "min", "max", "verdict"))
    print("-" * 52)
    for mv, field in ((3600, "min"), (3900, "max"), (3750, "both")):
        p.set_module_cell(0, 0, mv)
        time.sleep(2.5)
        mn, mx, _ = sample()
        if field == "min":
            ok = mn == mv and mx == BASE
        elif field == "max":
            ok = mx == mv and mn == BASE
        else:
            ok = mn == mx == BASE
        fails.append(None if ok else "cell %d" % mv)
        print("%12d | %6s %6s | %s" % (mv, mn, mx, "ok" if ok else "MISMATCH"))

    print("\n-- temperature path still live --")
    print("%12s | %-18s | %s" % ("set", "0x136 temps (C)", "verdict"))
    print("-" * 52)
    for dC in (250, 500, 100):
        p.set_all_temps(dC)
        time.sleep(2.5)
        _, _, temps = sample()
        ok = bool(temps) and all(abs(t - dC / 10) <= 1 for t in temps)
        fails.append(None if ok else "temp %d" % dC)
        print("%11.1fC | %-18s | %s" % (dC / 10, temps, "ok" if ok else "MISMATCH"))

    p.set_all_temps(250)
    st = p.status()
    print("\ntx_stall_cmd = %s   n_cs_cycles = %s   n_valid_cmds = %s"
          % (st.get("tx_stall_cmd"), st.get("n_cs_cycles"), st.get("n_valid_cmds")))

bad = [f for f in fails if f]
print("RESULT: %s" % ("PASS - all %d checks" % len(fails) if not bad
                      else "FAIL - " + ", ".join(bad)))
