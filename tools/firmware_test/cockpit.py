"""
Cockpit GPIO stimulus for the AMS firmware. Drives the two physical
inputs that took over the role of the retired 0x600 / 0x18FF50E7 CAN
frames (see isc-fs/IFS08-CE-AMS#187):

    TSMS    -- Tractive System Master Switch (PF9, active-high)
    RST_PIL -- Reset Pilot                   (PF10, active-high)

Both are wired to TCA9555 output pins on the BACKPLANE_HIL; this class
just hides the `tca.write_pin` RPC behind a clearer interface.

The pin assignments come from `ams_profile.yaml`; this class only
encodes the protocol (set HIGH = asserted, LOW = released, level-polled
by firmware at 20 ms cadence).

Example:

    from tools.firmware_test.cockpit import Cockpit
    with Cockpit(client, tsms=(0x21, 0, 0),
                          rst_pil=(0x21, 0, 1)) as c:
        c.deassert_all()
        ...
        c.assert_both()
"""

from __future__ import annotations

import logging
from typing import Tuple

log = logging.getLogger(__name__)

# (tca_addr, port, pin) — driven onto a TCA9555 output pin.
PinTriple = Tuple[int, int, int]


class Cockpit:
    """TCA9555-driven TSMS + RST_PIL helper.

    Constructed with a connected `BrokerClient` and the two pin triples.
    On enter / `start()`, both pins are configured as outputs and driven
    LOW. On exit / `stop()`, both pins are driven LOW (safe state) but
    direction is left unchanged so other fixtures can adopt them.
    """

    def __init__(self, client, *,
                 tsms: PinTriple,
                 rst_pil: PinTriple):
        self._client  = client
        self._tsms    = tsms
        self._rst_pil = rst_pil

    # -- lifecycle ------------------------------------------------------

    def start(self) -> "Cockpit":
        # Set both pins as outputs (bit=0 in the direction register
        # means output on TCA9555). Use write_pin's direction-set sibling
        # if the broker exposes it; otherwise direction is sticky from
        # whatever the previous client left it as. The bench's MLC-power
        # fixture already calls `tca.set_direction` for 0x20 port 0; we
        # mirror that pattern here.
        for (addr, port, pin) in (self._tsms, self._rst_pil):
            self._client.call("tca.set_direction",
                              addr=addr, port=port,
                              mask=self._direction_mask_with_output(addr, port, pin))
        self.deassert_all()
        log.info("cockpit: TSMS=tca0x%02x p%d.b%d, RST_PIL=tca0x%02x p%d.b%d",
                 *self._tsms, *self._rst_pil)
        return self

    def stop(self) -> None:
        try:
            self.deassert_all()
        except Exception as e:
            log.warning("cockpit: deassert_all on stop failed: %s", e)

    def __enter__(self) -> "Cockpit":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- assertion helpers ---------------------------------------------

    def assert_tsms(self) -> None:
        self._write(self._tsms, True)

    def deassert_tsms(self) -> None:
        self._write(self._tsms, False)

    def assert_rst_pil(self) -> None:
        self._write(self._rst_pil, True)

    def deassert_rst_pil(self) -> None:
        self._write(self._rst_pil, False)

    def assert_both(self) -> None:
        """Most common move: drive Start -> Precharge."""
        self.assert_tsms()
        self.assert_rst_pil()

    def deassert_all(self) -> None:
        """Safe state -- mid-Run this latches Error (sticky)."""
        self.deassert_tsms()
        self.deassert_rst_pil()

    # -- internal -------------------------------------------------------

    def _write(self, triple: PinTriple, value: bool) -> None:
        addr, port, pin = triple
        self._client.call("tca.write_pin",
                          addr=addr, port=port, pin=pin, value=bool(value))

    def _direction_mask_with_output(self, addr: int, port: int, pin: int) -> int:
        # TCA9555 direction register: 0 = output, 1 = input. We only know
        # *this* fixture's pin, so leave the others as inputs (the safe
        # default after a TCA9555 power-on). Other fixtures on the same
        # port + addr will need to re-assert their own direction bits.
        return (~(1 << pin)) & 0xFF
