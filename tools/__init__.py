"""Utility tooling for the HIL bench.

Driver classes are re-exported for convenience when the RPi hardware
dependencies (spidev, smbus2, RPi.GPIO) are available. Off-bench (CI,
laptop dev with the fake broker), the re-export is skipped so that
importing `tools.hw_config` or `tools.hil_client` still works.
"""

try:
    from tools.mcp3208  import MCP3208
    from tools.dac80504 import DAC80504
    from tools.mcp2515  import MCP2515
    from tools.ina226   import INA226
    from tools.tca9555  import TCA9555
    from tools.nrf24l01 import NRF24L01

    __all__ = [
        "MCP3208",
        "DAC80504",
        "MCP2515",
        "INA226",
        "TCA9555",
        "NRF24L01",
    ]
except ImportError:
    __all__ = []
