#!/usr/bin/env python3
"""IFS_HIL#116 signature probe.

Holds every cell at a fixed mV and steps the NTC temperature. If chain
position 0 is being served the PREVIOUS command's response, module 0
cell 0 tracks the AUX (NTC divider) voltage instead of the cell voltage.

  pre-fix : cell0 swings with temperature, min_cell != cell_mV
  post-fix: cell0 == cell_mV at every step
"""
import subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.pico_ltc_emulator.host.pico_ltc_client import PicoLtcClient

CELL_MV = 3750
STEPS   = [250, 500, 100, 250]          # deci-degC


def sample(iface="can2", secs=2.0):
    """Return ([cell0, cell1, cell2], min_cell) from 0x131 / 0x12C."""
    out = subprocess.run(["timeout", str(secs), "candump", iface],
                         capture_output=True, text=True).stdout
    c131 = c12c = None
    for ln in out.splitlines():
        f = ln.split()
        if len(f) < 4:
            continue
        if f[1] == "131":
            c131 = f[3:]
        elif f[1] == "12C":
            c12c = f[3:]

    def u16(b, i):
        return int(b[i], 16) << 8 | int(b[i + 1], 16)

    cells = [u16(c131, i) for i in (0, 2, 4)] if c131 and len(c131) >= 6 else [None] * 3
    mn = u16(c12c, 0) if c12c and len(c12c) >= 2 else None
    return cells, mn


with PicoLtcClient() as p:
    print("pico:", p.ping())
    p.set_all_cells(CELL_MV)
    time.sleep(1.0)
    print("\nall cells held at %d mV" % CELL_MV)
    print("%8s | %6s %6s %6s | %6s | verdict" % ("temp", "cell0", "cell1", "cell2", "min"))
    print("-" * 62)
    bad = 0
    for dC in STEPS:
        p.set_all_temps(dC)
        time.sleep(2.5)                       # let AMS re-scan + rebroadcast
        cells, mn = sample()
        ok = cells[0] == CELL_MV
        bad += 0 if ok else 1
        print("%7.1fC | %6s %6s %6s | %6s | %s"
              % (dC / 10, cells[0], cells[1], cells[2], mn,
                 "ok" if ok else "STALE (tracks AUX)"))
    st = p.status()
    print("\ntx_stall_cmd = %s   n_cs_cycles = %s   n_valid_cmds = %s"
          % (st.get("tx_stall_cmd", "n/a"), st.get("n_cs_cycles"), st.get("n_valid_cmds")))
    print("RESULT: %s" % ("PASS - chip 0 fresh at every step" if bad == 0
                          else "FAIL - chip 0 stale in %d/%d steps" % (bad, len(STEPS))))
