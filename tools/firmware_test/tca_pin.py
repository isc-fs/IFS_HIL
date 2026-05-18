"""
Single TCA9555 output-pin driver. Constructor takes a connected broker
client + the (addr, port, pin) triple identifying one output. Methods
just wrap `tca.write_pin` so test code reads cleanly.

Deliberately minimal — no multi-pin abstraction. AMS has two operator-
facing inputs (TSMS on the side of the car, DASH_CHG on the
dashboard) and they get one `TcaPin` instance each, exposed as the
separate `tsms` and `dash_chg` pytest fixtures in
`tests/hil/ams/conftest.py`. Nothing here lumps them together.

The direction register (output-vs-input) is managed by the fixture
that owns the pin; this class assumes the direction has already been
configured before any `set()` call.

Example:

    from broker.server import BrokerClient
    from tools.firmware_test.tca_pin import TcaPin
    client = BrokerClient("/run/hil-broker/broker.sock")
    pin = TcaPin(client, addr=0x21, port=0, pin=0, label="TSMS")
    pin.assert_()    # drive high
    pin.deassert()   # drive low
    pin.set(True)    # explicit form
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class TcaPin:
    """Wraps `tca.write_pin` for one TCA9555 output channel."""

    def __init__(self, client, addr: int, port: int, pin: int,
                 label: str = "tca-pin"):
        self._client = client
        self._addr   = addr
        self._port   = port
        self._pin    = pin
        self._label  = label

    @property
    def label(self) -> str:
        return self._label

    def set(self, value: bool) -> None:
        self._client.call("tca.write_pin",
                          addr=self._addr, port=self._port,
                          pin=self._pin, value=bool(value))

    def assert_(self) -> None:
        self.set(True)

    def deassert(self) -> None:
        self.set(False)
