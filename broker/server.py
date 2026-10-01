"""
Unix-socket front-end for the hil-broker.

Listens on SOCKET_PATH, reads newline-delimited JSON-RPC requests,
dispatches them through broker.rpc, and writes back responses on the
same connection. One reader thread per connection; the backend
serialises concurrent requests via its per-bus locks.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
from pathlib import Path
from typing import Callable

from broker.rpc import build_method_table, handle_request

DEFAULT_SOCKET_PATH = "/run/hil-broker/broker.sock"

log = logging.getLogger("hil-broker")


class _Handler(socketserver.StreamRequestHandler):
    methods: dict[str, Callable]  # bound in server factory

    def handle(self) -> None:
        peer = f"fd={self.connection.fileno()}"
        log.debug("client connected %s", peer)
        for raw in self.rfile:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            resp = handle_request(line, self.methods)
            if resp is not None:
                try:
                    self.wfile.write(resp.encode("utf-8"))
                    self.wfile.flush()
                except BrokenPipeError:
                    break
        log.debug("client disconnected %s", peer)


class _ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_server(socket_path: str, methods: dict[str, Callable]) -> _ThreadedUnixServer:
    path = Path(socket_path)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = type("BoundHandler", (_Handler,), {"methods": methods})
    server = _ThreadedUnixServer(socket_path, handler)
    os.chmod(socket_path, 0o660)
    return server


def serve(backend, socket_path: str = DEFAULT_SOCKET_PATH) -> None:
    methods = build_method_table(backend)
    server = _make_server(socket_path, methods)

    stop = threading.Event()

    def _on_signal(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

    log.info("hil-broker listening on %s", socket_path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            Path(socket_path).unlink(missing_ok=True)
        except OSError:
            pass
        log.info("hil-broker stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hil-broker daemon")
    parser.add_argument("--socket", default=DEFAULT_SOCKET_PATH,
                        help=f"Unix socket path (default: {DEFAULT_SOCKET_PATH})")
    parser.add_argument("--fake", action="store_true",
                        help="use the fake hardware backend (no /dev access)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.fake:
        from broker.fake_bus import FakeHardwareManager
        backend = FakeHardwareManager()
    else:
        from broker.bus import HardwareManager
        backend = HardwareManager()

    serve(backend, args.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# Convenience for clients: a tiny synchronous JSON-RPC client using the
# same socket. Kept here so there is one import path and no extra module.

class BrokerClient:
    """Synchronous client for the broker. One socket per instance;
    not thread-safe — create one per thread."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH, timeout: float = 5.0):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(socket_path)
        self._rfile = self._sock.makefile("r", encoding="utf-8")
        self._wfile = self._sock.makefile("w", encoding="utf-8")
        self._next_id = 0

    def call(self, method: str, **params):
        import json
        self._next_id += 1
        req = {"id": self._next_id, "method": method, "params": params}
        self._wfile.write(json.dumps(req) + "\n")
        self._wfile.flush()
        line = self._rfile.readline()
        if not line:
            raise ConnectionError("broker closed the connection")
        resp = json.loads(line)
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"[{err.get('code')}] {err.get('message')}")
        return resp.get("result")

    def close(self) -> None:
        try:
            self._rfile.close()
            self._wfile.close()
        finally:
            self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
