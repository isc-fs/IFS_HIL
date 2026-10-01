"""
CAN trace ring buffer + SSE feed for the dashboard.

Opens its OWN raw SocketCAN socket per kernel CAN interface (can0/can1/can2);
this is independent of the broker because AF_CAN sockets are not the
`/dev/spidev*` / `/dev/i2c-*` / `/dev/gpio*` devices the broker monopolises
(see CLAUDE.md's "six hard invariants"). Multiple SocketCAN readers on the
same interface each get all frames -- safe to coexist with pytest's
observer fixtures and with `can-flasher`.

Each frame is decoded to a small JSON blob with raw + decoded fields:

    {
        "ts_ms":   1742394283417,
        "bus":     "can0",
        "id":      "0x4A0",
        "ext":     false,
        "dlc":     8,
        "data":    "0500 1F07 0EA6 0EA6",   # space-grouped for readability
        "decoded": {                         # only for known AMS IDs
            "kind":  "ams_telem_status",
            "state": "Start",
            "ams_ok": false,
            "module_online_mask": 0x1F,
            ...
        }
    }
"""

from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
import time
from collections import deque
from typing import Iterable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AMS-aware decoding (mirrors tools/firmware_test/ams/can_map.py so we don't
# need to import that test-side module from the dashboard)
# ---------------------------------------------------------------------------

_FSM_NAMES = {0: "Start", 1: "Precharge", 2: "Transition",
              3: "Run",   4: "Charge",    5: "Error"}
_MODE_NAMES = {0: "Undecided", 1: "Car", 2: "Charger"}


def _decode_cockpit_byte(b: int) -> dict:
    if (b & 0x80) == 0:
        return {"valid": False, "raw": b}
    return {
        "valid":       True,
        "raw":         b,
        "mode_locked": _MODE_NAMES.get((b >> 2) & 0x03, "?"),
        "tsms":        bool(b & 0x02),
        "dash_chg":    bool(b & 0x01),
    }


def _i8(b: int) -> int:
    return b - 256 if b > 127 else b


def _decode_ams(can_id: int, data: bytes) -> dict | None:
    """Return a dict with decoded fields for known AMS IDs, else None."""
    if can_id == 0x4A0 and len(data) >= 8:
        return {
            "kind":              "ams_telem_status",
            "state":             _FSM_NAMES.get(data[0], f"0x{data[0]:02X}"),
            "ams_ok":            bool(data[1]),
            "module_online_mask": data[2],
            "min_cell_mV":       (data[4] << 8) | data[5],
            "max_cell_mV":       (data[6] << 8) | data[7],
        }
    if can_id == 0x4A1 and len(data) >= 8:
        return {
            "kind":             "ams_telem_pack",
            "pack_voltage_mV": int.from_bytes(data[0:4], "little", signed=False),
            "filtered_mA":    int.from_bytes(data[4:8], "little", signed=True),
        }
    if can_id == 0x4A2 and len(data) >= 8:
        return {
            "kind":      "ams_telem_temps",
            "min_tempC": _i8(data[0]),
            "max_tempC": _i8(data[1]),
            "avg_tempC": _i8(data[2]),
            "byte3":     data[3],
            "byte4":     data[4],
            "cockpit":   _decode_cockpit_byte(data[5]),
            "byte6":     data[6],
            "heartbeat": data[7],
        }
    if can_id == 0x100 and len(data) >= 2:
        return {
            "kind":      "vcu_dc_bus",
            "dc_bus_V":  data[0] | (data[1] << 8),
        }
    return None


# ---------------------------------------------------------------------------
# Tracer core
# ---------------------------------------------------------------------------

_CAN_EFF_FLAG = 0x80000000
_CAN_RTR_FLAG = 0x40000000
_CAN_ERR_FLAG = 0x20000000


def _format_data(data: bytes) -> str:
    """Hex-encode `data` grouped in 2-byte words for readability."""
    hexed = data.hex().upper()
    return " ".join(hexed[i:i+4] for i in range(0, len(hexed), 4))


class CanTracer:
    """Multi-bus passive CAN sniffer with subscriber fan-out.

    - One background thread per `interface` reading AF_CAN frames.
    - Each bus keeps a ring buffer of the last `history_per_bus` frames.
    - Live subscribers (SSE clients) get every frame pushed to their queue.
    """

    def __init__(self, interfaces: Iterable[str] = ("can0", "can1", "can2"),
                 history_per_bus: int = 1000):
        self._interfaces = tuple(interfaces)
        self._history: dict[str, deque] = {
            i: deque(maxlen=history_per_bus) for i in self._interfaces
        }
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._t_start = time.time()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        for iface in self._interfaces:
            t = threading.Thread(
                target=self._reader_loop, args=(iface,),
                name=f"can-tracer-{iface}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("CanTracer started on %s", ", ".join(self._interfaces))

    def stop(self) -> None:
        self._stop_evt.set()

    # -- subscribers ----------------------------------------------------

    def subscribe(self, maxsize: int = 256) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    # -- history --------------------------------------------------------

    def recent(self, bus: str | None = None, n: int = 200) -> list[dict]:
        """Snapshot of the last `n` frames on `bus` (or all buses if None)."""
        if bus is not None and bus in self._history:
            return list(self._history[bus])[-n:]
        out: list[dict] = []
        for iface in self._interfaces:
            out.extend(self._history[iface])
        out.sort(key=lambda f: f["ts_ms"])
        return out[-n:]

    # -- internals ------------------------------------------------------

    def _reader_loop(self, iface: str) -> None:
        try:
            sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            sock.bind((iface,))
            sock.settimeout(0.5)
        except OSError as exc:
            log.warning("CanTracer: cannot bind %s (%s); skipping", iface, exc)
            return

        while not self._stop_evt.is_set():
            try:
                raw = sock.recv(16)
            except (TimeoutError, socket.timeout):
                continue
            except OSError as exc:
                log.warning("CanTracer %s read err: %s", iface, exc)
                time.sleep(0.5)
                continue
            if len(raw) < 8:
                continue

            can_id_raw, dlc = struct.unpack("=IB3x", raw[:8])
            ext = bool(can_id_raw & _CAN_EFF_FLAG)
            err = bool(can_id_raw & _CAN_ERR_FLAG)
            if err:
                continue  # skip kernel error frames for now
            can_id = can_id_raw & (0x1FFFFFFF if ext else 0x7FF)
            data = bytes(raw[8:8 + dlc])

            with self._id_lock:
                self._next_id += 1
                seq = self._next_id

            frame = {
                "seq":     seq,
                "ts_ms":   int(time.time() * 1000),
                "bus":     iface,
                "id":      f"0x{can_id:0{8 if ext else 3}X}",
                "id_int":  can_id,
                "ext":     ext,
                "dlc":     dlc,
                "data":    _format_data(data),
                "decoded": _decode_ams(can_id, data),
            }

            self._history[iface].append(frame)

            # Fan-out to live subscribers; drop oldest on overflow so a
            # slow client never blocks the reader.
            with self._sub_lock:
                stale = []
                for q in self._subscribers:
                    try:
                        q.put_nowait(frame)
                    except queue.Full:
                        stale.append(q)
                for q in stale:
                    try: self._subscribers.remove(q)
                    except ValueError: pass

        sock.close()
