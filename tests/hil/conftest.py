"""
pytest fixtures for BACKPLANE_HIL hardware tests.

All tests in this directory require the physical PCB connected to a Raspberry Pi.
Run with: pytest tests/hil/ -v --tb=short

Pre-requisites on the RPi:
  sudo raspi-config  →  Interface Options → SPI (enable) → I2C (enable)
  sudo modprobe i2c-dev  (add 'i2c-dev' to /etc/modules for persistence)
  pip install spidev smbus2 RPi.GPIO pytest

NOTE: The SPI MISO buffer (IC1 SN74LVC125A) is hardware-controlled.
  Buffer ~OE is driven LOW by Q5 NMOS when ATX PWR_OK is HIGH.
  No GPIO management is required — just ensure PSU is on before SPI tests.
"""

import time
import pytest
import spidev
import smbus2
import RPi.GPIO as GPIO

from tools import hw_config as CFG
from tools.mcp3208  import MCP3208
from tools.dac80504 import DAC80504
from tools.mcp2515  import MCP2515
from tools.ina226   import INA226
from tools.tca9555  import TCA9555
from tools.nrf24l01 import NRF24L01


# ---------------------------------------------------------------------------
# GPIO + SPI setup / teardown
# ---------------------------------------------------------------------------

ALL_CS_PINS = [
    CFG.CS_CAN1, CFG.CS_CAN2, CFG.CS_CAN3,
    CFG.CS_ADC1, CFG.CS_ADC2, CFG.CS_ADC3,
    CFG.CS_DAC1, CFG.CS_DAC2, CFG.CS_DAC3, CFG.CS_DAC4,
    CFG.NRF24_CS,
]

OUTPUT_PINS = ALL_CS_PINS + [CFG.NRF24_CE]


@pytest.fixture(scope="session", autouse=True)
def gpio_setup():
    """Initialise BCM GPIO numbering and set all CS/CE pins to safe states."""
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    for pin in OUTPUT_PINS:
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.HIGH)   # CS deselected (active-low)

    # Input pins (CAN interrupts, nRF24 IRQ, power-good)
    for pin in [CFG.INT_CAN1, CFG.INT_CAN2, CFG.INT_CAN3, CFG.NRF24_IRQ]:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(CFG.PWR_OK, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    # SPI_GATE_OE is hardware-controlled (PWR_OK → Q5 NMOS → IC1 ~OE).
    # No GPIO action needed — buffer is enabled automatically when PSU is on.

    yield

    # Cleanup: drive all outputs safe, release GPIO
    for pin in OUTPUT_PINS:
        GPIO.output(pin, GPIO.HIGH)
    GPIO.cleanup()


@pytest.fixture(scope="session")
def psu_on(gpio_setup):
    """Power on the ATX PSU and wait for PWR_OK, then yield; power off on teardown."""
    GPIO.setup(CFG.PSU_ON, GPIO.OUT)
    GPIO.output(CFG.PSU_ON, GPIO.LOW)   # assert PS_ON# → PSU on

    import time
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if GPIO.input(CFG.PWR_OK):
            break
        time.sleep(0.05)

    if not GPIO.input(CFG.PWR_OK):
        pytest.fail(
            f"ATX PSU did not assert PWR_OK (GPIO{CFG.PWR_OK}) within 5 s. "
            "Check PSU cable and PS_ON# wiring."
        )

    time.sleep(0.3)   # let rails settle and MISO buffer enable
    yield
    # PSU is intentionally left ON after the test session ends.
    # De-assert manually (GPIO7 HIGH) only when the bench is being powered down.


@pytest.fixture(scope="session")
def spi_bus(gpio_setup, psu_on):
    """Open SPI0.0 at 1 MHz mode 0; yields the open SpiDev instance."""
    bus = spidev.SpiDev()
    bus.open(CFG.SPI_BUS, CFG.SPI_DEVICE)
    bus.max_speed_hz = CFG.SPI_MAX_HZ
    bus.mode = 0b00          # CPOL=0, CPHA=0
    bus.no_cs = True         # we manage CS manually
    bus.lsbfirst = False
    yield bus
    bus.close()


@pytest.fixture(scope="session")
def i2c_bus(gpio_setup, psu_on):
    """Open I2C bus 1 (/dev/i2c-1, GPIO2/GPIO3); yields the SMBus instance."""
    bus = smbus2.SMBus(CFG.I2C_BUS)
    time.sleep(0.05)
    yield bus
    bus.close()


# ---------------------------------------------------------------------------
# Per-device fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def adcs(spi_bus):
    return [
        MCP3208(spi_bus, CFG.CS_ADC1),
        MCP3208(spi_bus, CFG.CS_ADC2),
        MCP3208(spi_bus, CFG.CS_ADC3),
    ]


@pytest.fixture(scope="session")
def dacs(spi_bus):
    return [
        DAC80504(spi_bus, CFG.CS_DAC1),
        DAC80504(spi_bus, CFG.CS_DAC2),
        DAC80504(spi_bus, CFG.CS_DAC3),
        DAC80504(spi_bus, CFG.CS_DAC4),
    ]


@pytest.fixture(scope="session")
def can_controllers(spi_bus):
    return [
        MCP2515(spi_bus, CFG.CS_CAN1, CFG.MCP2515_OSC_HZ),
        MCP2515(spi_bus, CFG.CS_CAN2, CFG.MCP2515_OSC_HZ),
        MCP2515(spi_bus, CFG.CS_CAN3, CFG.MCP2515_OSC_HZ),
    ]


@pytest.fixture(scope="session")
def power_monitors(i2c_bus):
    return [
        INA226(i2c_bus, CFG.INA226_ADDR_12V,  CFG.INA226_SHUNT_OHM),
        INA226(i2c_bus, CFG.INA226_ADDR_5V,   CFG.INA226_SHUNT_OHM),
        INA226(i2c_bus, CFG.INA226_ADDR_3V3,  CFG.INA226_SHUNT_OHM),
        INA226(i2c_bus, CFG.INA226_ADDR_SBY,  CFG.INA226_SHUNT_OHM),
    ]


@pytest.fixture(scope="session")
def io_expanders(i2c_bus):
    return [
        TCA9555(i2c_bus, CFG.TCA9555_ADDR_0),
        TCA9555(i2c_bus, CFG.TCA9555_ADDR_1),
        TCA9555(i2c_bus, CFG.TCA9555_ADDR_2),
    ]


@pytest.fixture(scope="session")
def nrf24(spi_bus):
    return NRF24L01(spi_bus, CFG.NRF24_CS, CFG.NRF24_CE)
