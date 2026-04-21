"""
HIL client seam.

Phase 0 of the hardware-broker migration (see docs/broker_migration_plan.md).
Today this module re-exports the existing driver classes verbatim, so
`from tools.hil_client import DAC80504` behaves identically to
`from tools.dac80504 import DAC80504`.

In Phase 2 the internals flip: these names will point at thin proxy classes
that speak JSON-RPC to hil-broker over /run/hil-broker.sock. Callers that
migrate to this module now will not need to change again.

Do not add logic here in Phase 0. Keep it a pure re-export surface.
"""

from tools import hw_config as CFG
from tools.dac80504 import DAC80504
from tools.ina226 import INA226
from tools.mcp2515 import MCP2515
from tools.mcp3208 import MCP3208
from tools.nrf24l01 import NRF24L01
from tools.tca9555 import TCA9555

__all__ = [
    "CFG",
    "DAC80504",
    "INA226",
    "MCP2515",
    "MCP3208",
    "NRF24L01",
    "TCA9555",
]
