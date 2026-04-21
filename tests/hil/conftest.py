"""
pytest fixtures for BACKPLANE_HIL hardware tests.

As of Phase 3 of the broker migration, these tests talk to the bench
through hil-broker — they do NOT open /dev/spidev*, /dev/i2c-1, or
/dev/gpiochip0 themselves. Start the broker before running:

    sudo systemctl start hil-broker
    # or during dev:
    HIL_BROKER_SOCKET=/tmp/hil-broker.sock python3 -m broker.server \\
        --socket /tmp/hil-broker.sock &

Then:

    pytest tests/hil/ -v --tb=short

The suite and the dashboard can now run concurrently — the broker
serialises bus access across processes, so there is no contention.
"""

import os
import time

import pytest

from tools import hw_config as CFG
from tools.hil_client import (
    DAC80504, INA226, MCP2515, MCP3208, NRF24L01, TCA9555,
    close_client, get_client, psu_power, psu_status,
)


# ---------------------------------------------------------------------------
# Broker connectivity
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def broker_available():
    """Skip the whole session if the broker socket is not reachable."""
    try:
        client = get_client()
        client.call("broker.health")
    except Exception as exc:
        socket_path = os.environ.get("HIL_BROKER_SOCKET", "(default)")
        pytest.skip(f"hil-broker not reachable at {socket_path}: {exc}")
    yield
    close_client()


# ---------------------------------------------------------------------------
# PSU
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def psu_on(broker_available):
    """Ensure the ATX PSU is on and rails are stable for the test session."""
    status = psu_power(True)
    if not status.get("pwr_ok"):
        pytest.fail(
            "ATX PSU did not assert PWR_OK within 5 s. "
            "Check PSU cable and PS_ON# wiring."
        )
    time.sleep(0.3)  # let rails settle
    yield
    # PSU is intentionally left ON after the session.


# ---------------------------------------------------------------------------
# Legacy-shaped fixtures (placeholders — broker owns real SPI/I2C handles).
# Kept so test modules that list `spi_bus` or `i2c_bus` in their signature
# still resolve. The yielded value is a sentinel — tests that touched the
# raw bus are migrating to proxies instead.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def spi_bus(broker_available, psu_on):
    yield None


@pytest.fixture(scope="session")
def i2c_bus(broker_available, psu_on):
    yield None


# ---------------------------------------------------------------------------
# Per-device fixtures — now return broker proxies
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def adcs(psu_on):
    return [MCP3208(idx=0), MCP3208(idx=1), MCP3208(idx=2)]


@pytest.fixture(scope="session")
def dacs(psu_on):
    return [DAC80504(idx=0), DAC80504(idx=1), DAC80504(idx=2), DAC80504(idx=3)]


@pytest.fixture(scope="session")
def can_controllers(psu_on):
    return [MCP2515(idx=0), MCP2515(idx=1), MCP2515(idx=2)]


@pytest.fixture(scope="session")
def power_monitors(psu_on):
    return [
        INA226(addr=CFG.INA226_ADDR_MLC1),
        INA226(addr=CFG.INA226_ADDR_MLC2),
        INA226(addr=CFG.INA226_ADDR_MLC3),
        INA226(addr=CFG.INA226_ADDR_MLC4),
    ]


@pytest.fixture(scope="session")
def io_expanders(psu_on):
    return [
        TCA9555(addr=CFG.TCA9555_ADDR_0),
        TCA9555(addr=CFG.TCA9555_ADDR_1),
        TCA9555(addr=CFG.TCA9555_ADDR_2),
    ]


@pytest.fixture(scope="session")
def nrf24(psu_on):
    return NRF24L01()
