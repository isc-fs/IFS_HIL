"""
CAN bus observer — passively sniffs a SocketCAN interface and lets tests
assert on cadence, payload patterns, or frame presence/absence.

Example:

    with CanObserver(channel="can0") as obs:
        # … kick off some stimulus …
        time.sleep(2.5)
        frames = obs.frames(can_id=0x12C, extended=True)
        period = obs.mean_period(can_id=0x12C, extended=True)
        assert 450 <= period <= 550, f"min-cell-V TX cadence {period} ms off-target"
"""

from __future__ import annotations

import statistics
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import can


@dataclass
class CapturedFrame:
    timestamp: float        # epoch seconds
    can_id: int
    is_extended_id: bool
    dlc: int
    data: bytes


class CanObserver:
    """Background sniffer on one SocketCAN channel. Captures every frame seen
    into an in-memory list; queries filter by ID + extended flag."""

    def __init__(self, channel: str):
        self.channel = channel
        self._bus: Optional[can.BusABC] = None
        self._captured: List[CapturedFrame] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> "CanObserver":
        self._bus = can.Bus(interface="socketcan", channel=self.channel)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name=f"observer-{self.channel}",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._bus:
            try: self._bus.shutdown()
            except Exception: pass
            self._bus = None

    def __enter__(self) -> "CanObserver":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- queries --------------------------------------------------------

    def clear(self) -> None:
        with self._lock:
            self._captured.clear()

    def all_frames(self) -> List[CapturedFrame]:
        with self._lock:
            return list(self._captured)

    def frames(self, can_id: Optional[int] = None,
               extended: Optional[bool] = None,
               since: Optional[float] = None) -> List[CapturedFrame]:
        """Filter captured frames. `since` is an epoch timestamp; pass
        `time.time()` *before* the stimulus to drop pre-stimulus history."""
        with self._lock:
            out = self._captured if since is None else [
                f for f in self._captured if f.timestamp >= since]
            if can_id is not None:
                out = [f for f in out if f.can_id == can_id]
            if extended is not None:
                out = [f for f in out if f.is_extended_id == extended]
            return list(out)

    def count(self, can_id: int, *, extended: bool = False,
              since: Optional[float] = None) -> int:
        return len(self.frames(can_id=can_id, extended=extended, since=since))

    def mean_period_ms(self, can_id: int, *, extended: bool = False,
                       since: Optional[float] = None) -> Optional[float]:
        """Mean inter-arrival period in milliseconds, or None if < 2 frames."""
        fs = self.frames(can_id=can_id, extended=extended, since=since)
        if len(fs) < 2:
            return None
        deltas = [(fs[i].timestamp - fs[i-1].timestamp) * 1000.0
                  for i in range(1, len(fs))]
        return statistics.mean(deltas)

    def max_period_ms(self, can_id: int, *, extended: bool = False,
                      since: Optional[float] = None) -> Optional[float]:
        fs = self.frames(can_id=can_id, extended=extended, since=since)
        if len(fs) < 2:
            return None
        deltas = [(fs[i].timestamp - fs[i-1].timestamp) * 1000.0
                  for i in range(1, len(fs))]
        return max(deltas)

    def last(self, can_id: int, *, extended: bool = False) -> Optional[CapturedFrame]:
        fs = self.frames(can_id=can_id, extended=extended)
        return fs[-1] if fs else None

    # -- inner loop -----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._bus.recv(timeout=0.2)
            except Exception:
                continue
            if msg is None:
                continue
            cf = CapturedFrame(
                timestamp=msg.timestamp if msg.timestamp else time.time(),
                can_id=msg.arbitration_id,
                is_extended_id=msg.is_extended_id,
                dlc=msg.dlc,
                data=bytes(msg.data),
            )
            with self._lock:
                self._captured.append(cf)
