#!/usr/bin/env python3
"""
can_tx_periodic.py

Utility to send CAN frames periodically.

Features:
- CLI: interface, arbitration id, data bytes, period (ms), count, extended flag
- Uses python-can if available (preferred). Falls back to raw socket CAN (socketcan) on Linux.

Examples:
  # send 0x11 0x22 periodically every 100 ms on can0 (infinite):
  ./can_tx_periodic.py --iface can0 --id 0x123 --data 1122 --period 100

  # send 8 bytes, 10 times:
  ./can_tx_periodic.py -i can0 -a 0x7DF -d 02010C0000000000 -p 200 -c 10

Dependencies:
- Optional: python-can (recommended). If absent, script uses Linux socketcan via raw sockets.

Run with -h for help.
"""

from __future__ import annotations

import argparse
import logging
import time
import sys
from typing import Optional


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Enviar tramas CAN periódicas")
    p.add_argument("--iface", "-i", required=True, help="Interfaz CAN (ej. can0)")
    p.add_argument("--id", "-a", required=True, help="Arbitration ID en hex (ej. 0x123)")
    p.add_argument("--data", "-d", default="", help="Datos en hex (ej. 112233). Max 8 bytes.")
    p.add_argument("--period", "-p", type=float, default=100.0, help="Periodo en ms (float). 0 = enviar una vez")
    p.add_argument("--count", "-c", type=int, default=0, help="Número de envíos. 0 = infinito")
    p.add_argument("--extended", "-e", action="store_true", help="Usar ID extendido (29-bit)")
    p.add_argument("--loglevel", default="INFO", help="Nivel de logging (DEBUG, INFO, WARNING, ERROR)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="Imprimir frame(s) en lugar de enviarlos")
    return p.parse_args()


def _parse_hex_id(s: str) -> int:
    s = s.strip()
    try:
        return int(s, 0)
    except Exception:
        raise argparse.ArgumentTypeError(f"Invalid id: {s}")


def _parse_data(hexstr: str) -> bytes:
    hexstr = hexstr.strip()
    if hexstr == "":
        return b""
    if len(hexstr) % 2 != 0:
        raise ValueError("Data hex string must have even length")
    b = bytes.fromhex(hexstr)
    if len(b) > 8:
        raise ValueError("CAN data length must be <= 8 bytes")
    return b


class CanTransmitter:
    def __init__(self, iface: str):
        self.iface = iface
        self.backend = None

    def open(self):
        # Try python-can first
        try:
            import can  # type: ignore

            logging.debug("Using python-can backend (native)")
            bus = can.interface.Bus(bustype="socketcan", channel=self.iface)
            self.backend = ("python-can", bus)
            return
        except Exception as e:
            logging.debug("python-can not available or failed to open: %s", e)

        # Fallback to socket raw
        try:
            import socket
            import struct

            AF_CAN = getattr(socket, "AF_CAN")
            SOCK_RAW = getattr(socket, "SOCK_RAW")
            CAN_RAW = getattr(socket, "CAN_RAW")

            s = socket.socket(AF_CAN, SOCK_RAW, socket.CAN_RAW)
            s.bind((self.iface,))
            self.backend = ("socket", s)
            logging.debug("Using raw socket CAN backend on %s", self.iface)
            return
        except Exception as e:
            logging.debug("Raw socket fallback failed: %s", e)

        raise RuntimeError("No CAN backend available (install python-can or run on Linux with socketcan)")

    def send(self, arb_id: int, data: bytes, extended_id: bool = False) -> None:
        if self.backend is None:
            raise RuntimeError("Backend not opened")

        kind, obj = self.backend
        if kind == "python-can":
            bus = obj
            import can  # type: ignore
            msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=extended_id)
            bus.send(msg)
        elif kind == "socket":
            s = obj
            import socket
            import struct

            # struct can_frame: (can_id, can_dlc, data[8]) -> 'I B 3x 8s'  (but we'll pack manually)
            can_id = arb_id & 0x1FFFFFFF
            if extended_id:
                can_id |= socket.CAN_EFF_FLAG if hasattr(socket, "CAN_EFF_FLAG") else 0x80000000
            else:
                can_id &= 0x7FF

            dlc = len(data)
            data_padded = data + b"\x00" * (8 - dlc)
            # can_frame in linux: struct can_frame { can_id_t can_id; __u8 can_dlc; __u8 __pad; __u8 __res0; __u8 __res1; __u8 data[8]; } -> '=IB3x8s'
            frame = struct.pack("=IB3x8s", can_id, dlc, data_padded)
            s.send(frame)
        else:
            raise RuntimeError("Unknown backend")

    def close(self):
        if self.backend is None:
            return
        kind, obj = self.backend
        try:
            if kind == "python-can":
                obj.shutdown()
            elif kind == "socket":
                obj.close()
        except Exception:
            pass


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.loglevel.upper(), logging.INFO), format="%(asctime)s %(levelname)s: %(message)s")

    try:
        arb_id = _parse_hex_id(args.id)
    except Exception as e:
        logging.error("Invalid id: %s", e)
        return 2

    try:
        data = _parse_data(args.data)
    except Exception as e:
        logging.error("Invalid data: %s", e)
        return 2

    period_s = float(args.period) / 1000.0
    count = int(args.count)

    # If dry-run requested, just print what would be sent and exit
    if args.dry_run:
        logging.info("DRY RUN: interface=%s id=0x%X data=%s count=%s extended=%s", args.iface, arb_id, data.hex(), 'infinite' if count == 0 else count, args.extended)
        to_print = f"DRY RUN -> iface={args.iface} id=0x{arb_id:X} data={data.hex()} dlc={len(data)} extended={args.extended} period_s={period_s} count={count}"
        print(to_print)
        return 0

    tx = CanTransmitter(args.iface)
    try:
        tx.open()
    except Exception as e:
        logging.error("Failed to open CAN interface: %s", e)
        return 3

    logging.info("Sending to %s id=0x%X data=%s period=%.3fs count=%s extended=%s", args.iface, arb_id, data.hex(), period_s, 'infinite' if count == 0 else count, args.extended)

    sent = 0
    try:
        if period_s <= 0:
            # send once
            tx.send(arb_id, data, args.extended)
            logging.info("Sent 1 frame")
            return 0

        start = time.perf_counter()
        next_time = start
        while True:
            now = time.perf_counter()
            if now >= next_time:
                try:
                    tx.send(arb_id, data, args.extended)
                    sent += 1
                except Exception as e:
                    logging.error("Failed to send CAN frame: %s", e)
                    break
                next_time += period_s
                # respect count
                if count > 0 and sent >= count:
                    break
            else:
                # Sleep small amount to avoid busy loop
                time.sleep(min(0.001, next_time - now))
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    finally:
        tx.close()

    logging.info("Sent %d frames", sent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
