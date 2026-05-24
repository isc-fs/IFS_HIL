"""
HIL client — thin proxies over hil-broker.

Phase 2 of the broker migration (see docs/broker_migration_plan.md).
These classes mimic the method surface of the real drivers in tools/*.py
but delegate every bus operation to the broker process via JSON-RPC.

Callers replace

    from tools.dac80504 import DAC80504

with

    from tools.hil_client import DAC80504

and stop needing to construct their own spidev / smbus / GPIO handles.

CS pins and I2C addresses are still passed positionally so existing
`DAC80504(spi, CFG.CS_DAC1)` call sites only need the import change.
The spi/i2c argument is accepted but ignored — the broker already owns
the bus. New code should prefer the keyword forms (idx=..., addr=...).
"""

from __future__ import annotations

import os
import threading
from typing import Any

from tools import hw_config as CFG
from broker.server import BrokerClient, DEFAULT_SOCKET_PATH


# ---------------------------------------------------------------------------
# Shared client
# ---------------------------------------------------------------------------

_SOCKET_ENV = "HIL_BROKER_SOCKET"

# One client per thread. BrokerClient itself is not thread-safe because
# each call writes and reads on the same socket; sharing one across
# threads would interleave requests/responses.
_tls = threading.local()


def _socket_path() -> str:
    return os.environ.get(_SOCKET_ENV, DEFAULT_SOCKET_PATH)


def get_client() -> BrokerClient:
    client = getattr(_tls, "client", None)
    if client is None:
        client = BrokerClient(_socket_path())
        _tls.client = client
    return client


def close_client() -> None:
    client = getattr(_tls, "client", None)
    if client is not None:
        client.close()
        _tls.client = None


# ---------------------------------------------------------------------------
# CS-pin → broker index lookup for SPI peripherals
# ---------------------------------------------------------------------------

_ADC_CS_TO_IDX = {CFG.CS_ADC1: 0, CFG.CS_ADC2: 1, CFG.CS_ADC3: 2}
_DAC_CS_TO_IDX = {CFG.CS_DAC1: 0, CFG.CS_DAC2: 1, CFG.CS_DAC3: 2, CFG.CS_DAC4: 3}
_CAN_CS_TO_IDX = {CFG.CS_CAN1: 0, CFG.CS_CAN2: 1, CFG.CS_CAN3: 2}


def _call(method: str, **params: Any) -> Any:
    """RPC into the broker, with auto-recovery after a broker restart.

    Long-running clients (e.g. the dashboard's hardware-poll thread) cache
    a BrokerClient per thread for performance. When the broker is restarted
    out from under them (e.g. `systemctl restart hil-broker`), the cached
    socket becomes a poisoned FD: every subsequent call raises and the
    cache never heals on its own.

    Strategy:
      1. Try the call. On ANY disconnect-like error, clear the cached
         client unconditionally (even if .close() itself raises) and
         re-raise immediately. The next caller's `get_client()` will
         build a fresh connection.
      2. The retry happens naturally at the caller's polling cadence
         (the dashboard polls every 2 s; by the time the second poll
         after broker bounce fires, the broker is back up).

    We don't retry inside _call because:
      - The broker isn't always ready in the millisecond after the
        socket is reopened. Tight in-call retries hit BrokenPipeError
        AGAIN and leave another half-dead client in cache (the
        original bug we're fixing).
      - One failed poll per thread per broker bounce is acceptable;
        permanent silence is not.
    """
    DISCONNECT_ERRORS = (
        ConnectionError,     # BrokerClient.call() when broker closed cleanly
        BrokenPipeError,     # write into a half-closed FD
        OSError,             # generic socket errors (covers ConnectionResetError)
        ValueError,          # "I/O operation on closed file" from makefile()
    )
    try:
        return get_client().call(method, **params)
    except DISCONNECT_ERRORS:
        # Unconditionally drop the cached client. close_client() may
        # itself raise if the underlying socket is already torn down --
        # we don't care, just null the cache slot directly.
        try:
            close_client()
        except Exception:
            pass
        _tls.client = None
        raise


# ---------------------------------------------------------------------------
# MCP3208 — ADC
# ---------------------------------------------------------------------------


class MCP3208:
    def __init__(self, spi: Any = None, cs_pin: int | None = None, *,
                 idx: int | None = None):
        if idx is None:
            if cs_pin is None:
                raise TypeError("MCP3208 requires cs_pin or idx")
            idx = _ADC_CS_TO_IDX[cs_pin]
        self._idx = idx

    def read_raw(self, channel: int, differential: bool = False) -> int:
        return _call("adc.read", idx=self._idx, channel=channel)

    def read_voltage(self, channel: int) -> float:
        return _call("adc.read_voltage", idx=self._idx, channel=channel)

    def read_all(self) -> list[int]:
        return _call("adc.read_all", idx=self._idx)


# ---------------------------------------------------------------------------
# DAC80504
# ---------------------------------------------------------------------------


class DAC80504:
    def __init__(self, spi: Any = None, cs_pin: int | None = None,
                 vref: float = 3.3, *, idx: int | None = None):
        if idx is None:
            if cs_pin is None:
                raise TypeError("DAC80504 requires cs_pin or idx")
            idx = _DAC_CS_TO_IDX[cs_pin]
        self._idx = idx

    def set_voltage(self, channel: int, voltage: float) -> None:
        _call("dac.set_voltage", idx=self._idx, channel=channel, volts=float(voltage))

    def get_voltage(self, channel: int) -> float:
        return _call("dac.get_voltage", idx=self._idx, channel=channel)

    def read_device_id(self) -> int:
        return _call("dac.read_device_id", idx=self._idx)

    def reset(self) -> None:
        _call("dac.reset", idx=self._idx)

    def zero_all(self) -> None:
        _call("dac.zero_all", idx=self._idx)

    def set_all(self, voltage: float) -> None:
        for ch in range(4):
            self.set_voltage(ch, voltage)


# ---------------------------------------------------------------------------
# MCP2515 — CAN
# ---------------------------------------------------------------------------


class MCP2515:
    def __init__(self, spi: Any = None, cs_pin: int | None = None,
                 osc_hz: int | None = None, *, idx: int | None = None):
        if idx is None:
            if cs_pin is None:
                raise TypeError("MCP2515 requires cs_pin or idx")
            idx = _CAN_CS_TO_IDX[cs_pin]
        self._idx = idx

    def set_mode(self, mode: int, timeout: float = 0.1) -> bool:
        return _call("can.set_mode", idx=self._idx, mode=mode)

    def get_mode(self) -> int:
        return _call("can.get_mode", idx=self._idx)

    def read_error_counters(self) -> tuple[int, int]:
        tec, rec = _call("can.read_error_counters", idx=self._idx)
        return tec, rec

    def reset(self) -> None:
        _call("can.reset", idx=self._idx)

    def init(self, bitrate: int = 500_000) -> bool:
        return _call("can.init", idx=self._idx, bitrate=bitrate)

    def loopback_test(self, can_id: int = 0x123,
                      data: bytes = b"\xDE\xAD\xBE\xEF") -> bool:
        import base64
        return _call("can.loopback_test", idx=self._idx, can_id=can_id,
                     data_b64=base64.b64encode(data).decode("ascii"))

    def int_level(self) -> int:
        """Read the MCP2515 INT pin level through the broker (GPIO-owned)."""
        return _call("can.int_level", idx=self._idx)


# ---------------------------------------------------------------------------
# INA226
# ---------------------------------------------------------------------------


class INA226:
    def __init__(self, i2c: Any = None, addr: int = 0x40, shunt_ohm: float = 0.01):
        self._addr = addr

    def is_present(self) -> bool:
        return _call("ina.is_present", addr=self._addr)

    def bus_voltage(self) -> float:
        return _call("ina.bus_voltage", addr=self._addr)

    def shunt_voltage(self) -> float:
        return _call("ina.shunt_voltage", addr=self._addr)

    def current(self) -> float:
        return _call("ina.current", addr=self._addr)

    def power(self) -> float:
        return _call("ina.power", addr=self._addr)

    def snapshot(self) -> dict:
        return _call("ina.read", addr=self._addr)

    def read_manufacturer_id(self) -> int:
        return _call("ina.read_manufacturer_id", addr=self._addr)

    def read_die_id(self) -> int:
        return _call("ina.read_die_id", addr=self._addr)


# ---------------------------------------------------------------------------
# TCA9555
# ---------------------------------------------------------------------------


class TCA9555:
    def __init__(self, i2c: Any = None, addr: int = 0x20):
        self._addr = addr

    def is_present(self) -> bool:
        return _call("tca.is_present", addr=self._addr)

    def set_direction(self, port: int, mask: int) -> None:
        _call("tca.set_direction", addr=self._addr, port=port, mask=mask)

    def get_direction(self, port: int) -> int:
        return _call("tca.get_direction", addr=self._addr, port=port)

    def set_all_inputs(self) -> None:
        _call("tca.set_all_inputs", addr=self._addr)

    def set_all_outputs(self) -> None:
        _call("tca.set_all_outputs", addr=self._addr)

    def write_port(self, port: int, value: int) -> None:
        _call("tca.write_port", addr=self._addr, port=port, value=value)

    def write_all(self, port0_val: int, port1_val: int) -> None:
        _call("tca.write_all", addr=self._addr, p0=port0_val, p1=port1_val)

    def write_pin(self, port: int, pin: int, state: bool) -> None:
        _call("tca.write_pin", addr=self._addr, port=port, pin=pin, value=bool(state))

    def read_port(self, port: int) -> int:
        return _call("tca.read_port", addr=self._addr, port=port)

    def read_pin(self, port: int, pin: int) -> bool:
        return bool(self.read_port(port) & (1 << pin))

    def read_all(self) -> dict:
        return _call("tca.read", addr=self._addr)


# ---------------------------------------------------------------------------
# nRF24L01+
# ---------------------------------------------------------------------------


class NRF24L01:
    def __init__(self, spi: Any = None, cs_pin: int | None = None,
                 ce_pin: int | None = None):
        pass

    def is_present(self) -> bool:
        return _call("nrf.is_present")


# ---------------------------------------------------------------------------
# PSU helpers — not in the original driver set, but the dashboard uses
# GPIO directly for PS_ON / PWR_OK. Route those through the broker too.
# ---------------------------------------------------------------------------


def psu_power(on: bool) -> dict:
    return _call("psu.power", on=bool(on))


def psu_status() -> dict:
    return _call("psu.status")


def i2c_scan(start: int = 0x08, end: int = 0x78) -> list[int]:
    """Return a sorted list of addresses that ACK on the I2C bus."""
    return _call("i2c.scan", start=start, end=end)


__all__ = [
    "CFG",
    "DAC80504",
    "INA226",
    "MCP2515",
    "MCP3208",
    "NRF24L01",
    "TCA9555",
    "psu_power",
    "psu_status",
    "i2c_scan",
    "get_client",
    "close_client",
]
