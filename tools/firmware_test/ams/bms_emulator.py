"""
BMS slave emulator — responds to AMS voltage/temp polls on FDCAN2.

Without this running, the AMS firmware trips ERROR within `BMS_VOLTAGE_STALE_MS`
(1500 ms) of boot because the BMS modules never reply. This emulator pretends to
be all 5 modules at once, returning tunable cell voltages and temperatures.

Usage from a test:

    from tools.firmware_test.ams.bms_emulator import BmsEmulator

    with BmsEmulator(channel="can2") as emu:
        emu.set_cell(module=2, cell=5, mV=2700)      # trip undervoltage
        # … run the test, then context exit stops the emulator

Usage from the command line (e.g. for ad-hoc bench checks):

    python3 -m tools.firmware_test.ams.bms_emulator --channel can2 --modules 5

Run on the Pi where `can2` is the SocketCAN interface for FDCAN2.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import can

from tools.firmware_test.ams import can_map as M

log = logging.getLogger(__name__)


@dataclass
class BmsSlave:
    """One BMS slave module. Holds tunable cell V and temp values, knows how
    to build the response frames its poll IDs should answer."""

    module: int
    cell_mV: list = field(default_factory=lambda: [3700] * M.CELLS_PER_MODULE)
    temp_C: list  = field(default_factory=lambda: [25]   * M.TEMPS_PER_MODULE)

    def canid(self) -> int:
        return M.bms_canid(self.module)

    # -- voltage --------------------------------------------------------

    def voltage_response_frames(self) -> list:
        """Five frames covering 19 cells. Last frame has 3 cells, bytes 6–7 unused."""
        frames = []
        for fi in range(5):
            buf = bytearray(8)
            cells_in_frame = self.cell_mV[fi * M.CELLS_PER_FRAME:(fi + 1) * M.CELLS_PER_FRAME]
            for k, mv in enumerate(cells_in_frame):
                buf[2 * k]     = (mv >> 8) & 0xFF
                buf[2 * k + 1] = mv & 0xFF
            frames.append(can.Message(
                arbitration_id=M.bms_voltage_resp_id(self.module, fi),
                data=bytes(buf), is_extended_id=False))
        return frames

    # -- temperature ----------------------------------------------------

    def temperature_response_frames(self) -> list:
        """Five frames covering 38 temps, signed int8 °C per byte."""
        frames = []
        for fi in range(5):
            buf = bytearray(8)
            temps_in_frame = self.temp_C[fi * M.TEMPS_PER_FRAME:(fi + 1) * M.TEMPS_PER_FRAME]
            for k, t in enumerate(temps_in_frame):
                buf[k] = t & 0xFF if t >= 0 else (256 + t) & 0xFF
            frames.append(can.Message(
                arbitration_id=M.bms_temp_resp_id(self.module, fi),
                data=bytes(buf), is_extended_id=False))
        return frames


class BmsEmulator:
    """Receives poll frames on FDCAN2, replies with the appropriate burst of
    response frames. Runs in a background thread; use as a context manager.

    Thread-safety: cell/temp setters take the lock; the run loop takes it
    around each response burst. A test that mutates values is therefore
    guaranteed to send a coherent snapshot, not a half-updated state.
    """

    def __init__(self, channel: str = M.FDCAN2_KERNEL,
                 num_modules: int = M.NUM_BMS_MODULES):
        self.channel = channel
        self.slaves: list[BmsSlave] = [BmsSlave(m) for m in range(num_modules)]
        self._lookup: Dict[int, Tuple[str, BmsSlave]] = {}
        for s in self.slaves:
            self._lookup[M.bms_voltage_poll_id(s.module)] = ("voltage", s)
            self._lookup[M.bms_temp_poll_id(s.module)]    = ("temperature", s)

        self._bus: Optional[can.BusABC] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stats = {"voltage_replies": 0, "temp_replies": 0, "unknown_rx": 0}

    # -- public mutators -----------------------------------------------

    def set_cell(self, module: int, cell: int, mV: int) -> None:
        with self._lock:
            self.slaves[module].cell_mV[cell] = int(mV)

    def set_all_cells(self, mV: int) -> None:
        with self._lock:
            for s in self.slaves:
                s.cell_mV = [int(mV)] * M.CELLS_PER_MODULE

    def set_temp(self, module: int, sensor: int, C: int) -> None:
        with self._lock:
            self.slaves[module].temp_C[sensor] = int(C)

    def set_all_temps(self, C: int) -> None:
        with self._lock:
            for s in self.slaves:
                s.temp_C = [int(C)] * M.TEMPS_PER_MODULE

    def stop_module(self, module: int) -> None:
        """Stop responding for one module (simulates a dead slave)."""
        for pid in (M.bms_voltage_poll_id(module), M.bms_temp_poll_id(module)):
            self._lookup.pop(pid, None)

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> "BmsEmulator":
        self._bus = can.Bus(interface="socketcan", channel=self.channel)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bms-emu", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._bus:
            try: self._bus.shutdown()
            except Exception: pass
            self._bus = None

    def __enter__(self) -> "BmsEmulator":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- inner loop -----------------------------------------------------

    def _run(self) -> None:
        log.info("BMS emulator listening on %s (%d modules)", self.channel, len(self.slaves))
        while not self._stop.is_set():
            try:
                msg = self._bus.recv(timeout=0.2)
            except Exception as e:
                log.warning("recv error: %s", e); continue
            if msg is None: continue
            if msg.is_extended_id: continue   # all BMS polls are standard

            entry = self._lookup.get(msg.arbitration_id)
            if entry is None:
                self._stats["unknown_rx"] += 1
                continue

            kind, slave = entry
            with self._lock:
                frames = (slave.voltage_response_frames() if kind == "voltage"
                          else slave.temperature_response_frames())
            for f in frames:
                try:
                    self._bus.send(f, timeout=0.1)
                except can.CanError as e:
                    log.warning("send error (%s): %s", kind, e)
                    break
            else:
                self._stats[f"{'voltage' if kind == 'voltage' else 'temp'}_replies"] += 1


def _cli() -> int:
    parser = argparse.ArgumentParser(description="AMS BMS slave emulator")
    parser.add_argument("--channel", default=M.FDCAN2_KERNEL,
                        help=f"SocketCAN channel (default: {M.FDCAN2_KERNEL})")
    parser.add_argument("--modules", type=int, default=M.NUM_BMS_MODULES)
    parser.add_argument("--cell-mv", type=int, default=3700,
                        help="Default cell voltage in mV applied to every cell")
    parser.add_argument("--temp-c", type=int, default=25,
                        help="Default temperature in °C applied to every sensor")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    emu = BmsEmulator(channel=args.channel, num_modules=args.modules)
    emu.set_all_cells(args.cell_mv)
    emu.set_all_temps(args.temp_c)
    emu.start()
    log.info("Press Ctrl-C to stop")
    try:
        while True:
            time.sleep(2.0)
            log.info("stats=%s", emu.stats)
    except KeyboardInterrupt:
        pass
    finally:
        emu.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
