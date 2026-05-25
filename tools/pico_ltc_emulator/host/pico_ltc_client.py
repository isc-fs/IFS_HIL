"""Host-side client for pico_ltc_emulator. Wraps the USB-CDC serial
command protocol with a Python class so tests and ad-hoc scripts don't
have to format command strings by hand.

Usage:

    from tools.pico_ltc_emulator.host.pico_ltc_client import PicoLtcClient

    with PicoLtcClient() as p:
        p.ping()                              # 'PONG 0.1.0'
        p.set_cell(module=0, cell=0, mV=4200) # raise cell 0 of LTC 0
        p.set_all_cells(3750)                 # back to default seed
        p.reset_state()
        s = p.status()                        # parsed dict

To re-flash without pressing BOOTSEL:

    p.reboot_to_bootloader()
    # then run the flash script which copies the new UF2 onto the
    # MSC mount that just appeared.

The serial port auto-resolves to the first /dev/ttyACM* whose USB
descriptor identifies it as a Pi Pico (RP2040 vendor 0x2E8A). Override
with `port=...` if multiple Picos are attached or you want a specific
device path.
"""
from __future__ import annotations

import glob
import logging
import time
from contextlib import contextmanager

try:
    import serial
except ImportError as exc:                            # pragma: no cover
    raise ImportError(
        "pyserial required: `pip install pyserial`"
    ) from exc

log = logging.getLogger(__name__)

PICO_VID = 0x2E8A   # Raspberry Pi
PICO_PID_CDC = 0x000A  # default RP2040 CDC

DEFAULT_BAUD = 115200       # USB-CDC ignores rate, kept for compat
DEFAULT_TIMEOUT_S = 1.0


def _find_pico_port() -> str:
    """Pick the first /dev/ttyACM* whose udev metadata matches a Pico."""
    try:
        from serial.tools import list_ports
    except ImportError:                              # pragma: no cover
        list_ports = None

    if list_ports is not None:
        for p in list_ports.comports():
            if (p.vid, p.pid) == (PICO_VID, PICO_PID_CDC):
                return p.device
        for p in list_ports.comports():
            if p.vid == PICO_VID:
                return p.device

    # Fallback: glob /dev/ttyACM*. Pi typically only has the Pico
    # CDC enumerating, so this is usually unambiguous.
    candidates = sorted(glob.glob("/dev/ttyACM*"))
    if candidates:
        return candidates[0]
    raise RuntimeError("No /dev/ttyACM* port found; is the Pico connected?")


class PicoLtcClient:
    def __init__(self, port: str | None = None,
                 baudrate: int = DEFAULT_BAUD,
                 timeout: float = DEFAULT_TIMEOUT_S):
        self.port = port or _find_pico_port()
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser: serial.Serial | None = None

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "PicoLtcClient":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        if self._ser is not None:
            return
        self._ser = serial.Serial(self.port, self.baudrate,
                                  timeout=self.timeout)
        # Pico echoes nothing until it sees a newline; a fresh port may
        # have stale bytes from the previous session — drain.
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    # ------------------------------------------------------------------
    # raw transport
    # ------------------------------------------------------------------

    def _send_recv(self, line: str) -> str:
        """Send `line\\n`, return the first response line stripped."""
        if self._ser is None:
            raise RuntimeError("port not open; call .open() or use as ctx mgr")
        payload = (line.rstrip() + "\n").encode("ascii")
        self._ser.write(payload)
        self._ser.flush()
        reply = self._ser.readline().decode("ascii", errors="replace").rstrip()
        if reply.startswith("ERR"):
            raise RuntimeError(f"Pico rejected `{line}`: {reply}")
        return reply

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def ping(self) -> str:
        """Returns e.g. 'PONG 0.1.0'."""
        return self._send_recv("PING")

    def status(self) -> dict:
        """Parse `OK k=v k=v ...` into a dict."""
        line = self._send_recv("STATUS")
        if not line.startswith("OK"):
            raise RuntimeError(f"unexpected: {line}")
        out: dict[str, int | str] = {}
        for tok in line.split()[1:]:
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            try:
                out[k] = int(v, 0)
            except ValueError:
                out[k] = v
        return out

    def set_cell(self, module: int, cell: int, mV: int) -> None:
        self._send_recv(f"SET_CELL {module} {cell} {mV}")

    def set_temp(self, module: int, sensor: int, deci_degC: int) -> None:
        self._send_recv(f"SET_TEMP {module} {sensor} {deci_degC}")

    def set_all_cells(self, mV: int) -> None:
        self._send_recv(f"SET_ALL_CELLS {mV}")

    def set_all_temps(self, deci_degC: int) -> None:
        self._send_recv(f"SET_ALL_TEMPS {deci_degC}")

    def reset_state(self) -> None:
        self._send_recv("RESET_STATE")

    def reboot_to_bootloader(self) -> None:
        """Trigger reset_usb_boot() on the Pico. After this call the
        device re-enumerates as USB MSC; the CDC port goes away. Wait
        ~2 s before invoking picotool against it."""
        if self._ser is None:
            return
        try:
            self._ser.write(b"BSL\n")
            self._ser.flush()
            # The Pico replies with 'OK -- bye' before rebooting; try
            # to consume it but don't fail if the port dies first.
            try:
                self._ser.readline()
            except (OSError, serial.SerialException):
                pass
        finally:
            self.close()

    # ------------------------------------------------------------------
    # convenience helpers
    # ------------------------------------------------------------------

    @contextmanager
    def temporarily(self, *, cell_overrides: dict | None = None,
                    temp_overrides: dict | None = None):
        """Apply cell/temp overrides for the duration of the block, then
        reset everything to defaults.

          cell_overrides: {(module, cell): mV, ...}
          temp_overrides: {(module, sensor): deci_degC, ...}
        """
        try:
            for (m, c), mV in (cell_overrides or {}).items():
                self.set_cell(m, c, mV)
            for (m, s), dC in (temp_overrides or {}).items():
                self.set_temp(m, s, dC)
            yield
        finally:
            self.reset_state()
