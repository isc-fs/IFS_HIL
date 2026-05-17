"""
ACU bus (FDCAN1) stimulus helpers — DC bus voltage frames. Used to drive
the AMS firmware's freshness predicate + precharge logic from the bench.

Generic: not AMS-specific. The frame IDs themselves come from the AMS CAN
map but the transmit path is just SocketCAN. VCU/MicroDV tests can use the
same helpers with different frame IDs.

The `send_start_button` / `send_charger_detect` helpers were retired in
isc-fs/IFS08-CE-AMS#187 -- the FSM no longer consumes 0x600 or
0x18FF50E7. Cockpit triggers (TSMS, RST_PIL) are now GPIO inputs driven
via `tools.firmware_test.cockpit.Cockpit`.

Example:

    from tools.firmware_test.acu_stim import AcuStim

    with AcuStim(channel="can0") as acu:
        acu.send_dc_bus_v(350)
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import can

log = logging.getLogger(__name__)


class AcuStim:
    """ACU CAN stimulus on FDCAN1 (kernel `can0` on the IFS08_HIL bench)."""

    # Frame IDs — also exported here so callers don't have to import can_map
    DC_BUS_VOLTAGE_ID = 0x100         # ext, DLC 2, little-endian volts

    def __init__(self, channel: str = "can0"):
        self.channel = channel
        self._bus: Optional[can.BusABC] = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> "AcuStim":
        self._bus = can.Bus(interface="socketcan", channel=self.channel)
        return self

    def stop(self) -> None:
        if self._bus is not None:
            try: self._bus.shutdown()
            except Exception: pass
            self._bus = None

    def __enter__(self) -> "AcuStim":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- frame senders --------------------------------------------------

    def send_dc_bus_v(self, volts: int) -> None:
        """Emit 0x100 ext with DC bus voltage. Payload is little-endian uint16
        in volts (per CAN_MAP.md decode `(buf[1] << 8) | buf[0]`)."""
        v = int(volts) & 0xFFFF
        payload = bytes([v & 0xFF, (v >> 8) & 0xFF])
        self._send(self.DC_BUS_VOLTAGE_ID, payload, is_extended_id=True)

    def send_raw(self, can_id: int, data: bytes, *, is_extended_id: bool = False) -> None:
        """Escape hatch for ad-hoc frames."""
        self._send(can_id, data, is_extended_id=is_extended_id)

    # -- continuous emitters (for cadence-dependent tests) -------------

    def emit_dc_bus_v_periodic(self, volts_fn, period_s: float, stop_event):
        """Emit DC bus frames at `period_s` intervals until `stop_event` is set.
        `volts_fn` is called per iteration and may return a changing value."""
        while not stop_event.is_set():
            v = volts_fn() if callable(volts_fn) else volts_fn
            self.send_dc_bus_v(v)
            time.sleep(period_s)

    # -- internal -------------------------------------------------------

    def _send(self, can_id: int, data: bytes, *, is_extended_id: bool) -> None:
        if self._bus is None:
            raise RuntimeError("AcuStim used before start()")
        msg = can.Message(arbitration_id=can_id, data=data,
                          is_extended_id=is_extended_id)
        self._bus.send(msg, timeout=0.2)
