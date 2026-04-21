"""
Hardware manager — owns the SPI/I2C handles and GPIO, and exposes
thread-safe methods that the RPC dispatcher calls directly.

Every bus has its own lock. SPI transactions and I2C transactions are
independent and may run in parallel. GPIO ops on non-CS pins (PSU_ON,
PWR_OK) don't need the SPI lock; CS pins are touched only inside driver
calls, which already run under the SPI lock.

This module is the *only* place that opens /dev/spidev*, /dev/i2c-1,
or /dev/gpiochip0 on the bench once Phase 2 is complete.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol

from tools import hw_config as CFG


class HardwareBackend(Protocol):
    """Minimum surface the RPC dispatcher relies on. Both the real
    HardwareManager and FakeHardwareManager satisfy this."""

    # ADC
    def adc_read(self, idx: int, channel: int) -> int: ...
    def adc_read_all(self, idx: int) -> list[int]: ...
    def adc_read_voltage(self, idx: int, channel: int) -> float: ...

    # DAC
    def dac_set_voltage(self, idx: int, channel: int, volts: float) -> None: ...
    def dac_get_voltage(self, idx: int, channel: int) -> float: ...

    # CAN
    def can_set_mode(self, idx: int, mode: int) -> bool: ...
    def can_get_mode(self, idx: int) -> int: ...
    def can_read_error_counters(self, idx: int) -> list[int]: ...
    def can_status(self, idx: int) -> dict: ...

    # INA226
    def ina_read(self, addr: int) -> dict: ...
    def ina_is_present(self, addr: int) -> bool: ...
    def ina_bus_voltage(self, addr: int) -> float: ...
    def ina_shunt_voltage(self, addr: int) -> float: ...
    def ina_current(self, addr: int) -> float: ...
    def ina_power(self, addr: int) -> float: ...

    # TCA9555
    def tca_read(self, addr: int) -> dict: ...
    def tca_is_present(self, addr: int) -> bool: ...
    def tca_read_port(self, addr: int, port: int) -> int: ...
    def tca_set_direction(self, addr: int, port: int, mask: int) -> None: ...
    def tca_write_port(self, addr: int, port: int, value: int) -> None: ...
    def tca_write_pin(self, addr: int, port: int, pin: int, value: bool) -> None: ...

    # nRF24L01+
    def nrf_is_present(self) -> bool: ...

    # PSU
    def psu_power(self, on: bool) -> dict: ...
    def psu_status(self) -> dict: ...

    # Meta
    def health(self) -> dict: ...


class HardwareManager:
    """Real hardware backend. Instantiated once per broker process."""

    def __init__(self) -> None:
        # Lazy imports so this module is importable on non-RPi hosts.
        import RPi.GPIO as GPIO  # noqa: N814
        import smbus2
        import spidev

        from tools.dac80504 import DAC80504
        from tools.ina226 import INA226
        from tools.mcp2515 import MCP2515
        from tools.mcp3208 import MCP3208
        from tools.nrf24l01 import NRF24L01
        from tools.tca9555 import TCA9555

        self._GPIO = GPIO
        self._spi_lock = threading.Lock()
        self._i2c_lock = threading.Lock()
        self._gpio_lock = threading.Lock()
        self._started_at = time.time()
        self._op_count = 0
        self._last_error: str | None = None

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        cs_pins = [
            CFG.CS_CAN1, CFG.CS_CAN2, CFG.CS_CAN3,
            CFG.CS_ADC1, CFG.CS_ADC2, CFG.CS_ADC3,
            CFG.CS_DAC1, CFG.CS_DAC2, CFG.CS_DAC3, CFG.CS_DAC4,
            CFG.NRF24_CS,
        ]
        for pin in cs_pins + [CFG.NRF24_CE]:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.HIGH)
        for pin in [CFG.INT_CAN1, CFG.INT_CAN2, CFG.INT_CAN3, CFG.NRF24_IRQ]:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(CFG.PWR_OK, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        GPIO.setup(CFG.PSU_ON, GPIO.OUT)
        GPIO.output(CFG.PSU_ON, GPIO.HIGH)  # PSU off by default; client turns it on

        spi = spidev.SpiDev()
        spi.open(CFG.SPI_BUS, CFG.SPI_DEVICE)
        spi.max_speed_hz = CFG.SPI_MAX_HZ
        spi.mode = 0b00
        spi.no_cs = True
        spi.lsbfirst = False
        self._spi = spi

        self._i2c = smbus2.SMBus(CFG.I2C_BUS)

        # Driver instances. DAC80504 writes init registers in __init__, so
        # construction requires PSU on. We defer DAC construction until first
        # use to let the caller bring up the PSU explicitly.
        self._adcs = [
            MCP3208(spi, CFG.CS_ADC1),
            MCP3208(spi, CFG.CS_ADC2),
            MCP3208(spi, CFG.CS_ADC3),
        ]
        self._cans = [
            MCP2515(spi, CFG.CS_CAN1, CFG.MCP2515_OSC_HZ),
            MCP2515(spi, CFG.CS_CAN2, CFG.MCP2515_OSC_HZ),
            MCP2515(spi, CFG.CS_CAN3, CFG.MCP2515_OSC_HZ),
        ]
        self._dacs: list | None = None  # constructed on first DAC call
        self._DAC80504 = DAC80504

        self._inas = {
            CFG.INA226_ADDR_MLC1: INA226(self._i2c, CFG.INA226_ADDR_MLC1, CFG.INA226_SHUNT_OHM),
            CFG.INA226_ADDR_MLC2: INA226(self._i2c, CFG.INA226_ADDR_MLC2, CFG.INA226_SHUNT_OHM),
            CFG.INA226_ADDR_MLC3: INA226(self._i2c, CFG.INA226_ADDR_MLC3, CFG.INA226_SHUNT_OHM),
            CFG.INA226_ADDR_MLC4: INA226(self._i2c, CFG.INA226_ADDR_MLC4, CFG.INA226_SHUNT_OHM),
        }
        self._tcas = {
            CFG.TCA9555_ADDR_0: TCA9555(self._i2c, CFG.TCA9555_ADDR_0),
            CFG.TCA9555_ADDR_1: TCA9555(self._i2c, CFG.TCA9555_ADDR_1),
            CFG.TCA9555_ADDR_2: TCA9555(self._i2c, CFG.TCA9555_ADDR_2),
        }

        self._nrf = NRF24L01(spi, CFG.NRF24_CS, CFG.NRF24_CE)

    # ------------------------------------------------------------------
    # Lazy DAC initialisation (requires PSU on)
    # ------------------------------------------------------------------

    def _ensure_dacs(self) -> list:
        if self._dacs is None:
            with self._spi_lock:
                if self._dacs is None:
                    self._dacs = [
                        self._DAC80504(self._spi, CFG.CS_DAC1),
                        self._DAC80504(self._spi, CFG.CS_DAC2),
                        self._DAC80504(self._spi, CFG.CS_DAC3),
                        self._DAC80504(self._spi, CFG.CS_DAC4),
                    ]
        return self._dacs

    # ------------------------------------------------------------------
    # ADC
    # ------------------------------------------------------------------

    def adc_read(self, idx: int, channel: int) -> int:
        with self._spi_lock:
            self._op_count += 1
            return self._adcs[idx].read_raw(channel)

    def adc_read_all(self, idx: int) -> list[int]:
        with self._spi_lock:
            self._op_count += 1
            return self._adcs[idx].read_all()

    def adc_read_voltage(self, idx: int, channel: int) -> float:
        with self._spi_lock:
            self._op_count += 1
            return self._adcs[idx].read_voltage(channel)

    # ------------------------------------------------------------------
    # DAC
    # ------------------------------------------------------------------

    def dac_set_voltage(self, idx: int, channel: int, volts: float) -> None:
        dacs = self._ensure_dacs()
        with self._spi_lock:
            self._op_count += 1
            dacs[idx].set_voltage(channel, volts)

    def dac_get_voltage(self, idx: int, channel: int) -> float:
        dacs = self._ensure_dacs()
        with self._spi_lock:
            self._op_count += 1
            return dacs[idx].get_voltage(channel)

    # ------------------------------------------------------------------
    # CAN
    # ------------------------------------------------------------------

    def can_set_mode(self, idx: int, mode: int) -> bool:
        with self._spi_lock:
            self._op_count += 1
            return self._cans[idx].set_mode(mode)

    def can_get_mode(self, idx: int) -> int:
        with self._spi_lock:
            self._op_count += 1
            return self._cans[idx].get_mode()

    def can_read_error_counters(self, idx: int) -> list[int]:
        with self._spi_lock:
            self._op_count += 1
            tec, rec = self._cans[idx].read_error_counters()
            return [tec, rec]

    def can_status(self, idx: int) -> dict:
        with self._spi_lock:
            self._op_count += 1
            tec, rec = self._cans[idx].read_error_counters()
            return {"mode": self._cans[idx].get_mode(), "tec": tec, "rec": rec}

    # ------------------------------------------------------------------
    # INA226
    # ------------------------------------------------------------------

    def ina_read(self, addr: int) -> dict:
        with self._i2c_lock:
            self._op_count += 1
            return self._inas[addr].snapshot()

    def ina_is_present(self, addr: int) -> bool:
        with self._i2c_lock:
            self._op_count += 1
            return self._inas[addr].is_present()

    def ina_bus_voltage(self, addr: int) -> float:
        with self._i2c_lock:
            self._op_count += 1
            return self._inas[addr].bus_voltage()

    def ina_shunt_voltage(self, addr: int) -> float:
        with self._i2c_lock:
            self._op_count += 1
            return self._inas[addr].shunt_voltage()

    def ina_current(self, addr: int) -> float:
        with self._i2c_lock:
            self._op_count += 1
            return self._inas[addr].current()

    def ina_power(self, addr: int) -> float:
        with self._i2c_lock:
            self._op_count += 1
            return self._inas[addr].power()

    # ------------------------------------------------------------------
    # TCA9555
    # ------------------------------------------------------------------

    def tca_read(self, addr: int) -> dict:
        with self._i2c_lock:
            self._op_count += 1
            return self._tcas[addr].read_all()

    def tca_is_present(self, addr: int) -> bool:
        with self._i2c_lock:
            self._op_count += 1
            return self._tcas[addr].is_present()

    def tca_read_port(self, addr: int, port: int) -> int:
        with self._i2c_lock:
            self._op_count += 1
            return self._tcas[addr].read_port(port)

    def tca_set_direction(self, addr: int, port: int, mask: int) -> None:
        with self._i2c_lock:
            self._op_count += 1
            self._tcas[addr].set_direction(port, mask)

    def tca_write_port(self, addr: int, port: int, value: int) -> None:
        with self._i2c_lock:
            self._op_count += 1
            self._tcas[addr].write_port(port, value)

    def tca_write_pin(self, addr: int, port: int, pin: int, value: bool) -> None:
        with self._i2c_lock:
            self._op_count += 1
            self._tcas[addr].write_pin(port, pin, value)

    # ------------------------------------------------------------------
    # nRF24L01+
    # ------------------------------------------------------------------

    def nrf_is_present(self) -> bool:
        with self._spi_lock:
            self._op_count += 1
            return self._nrf.is_present()

    # ------------------------------------------------------------------
    # PSU
    # ------------------------------------------------------------------

    def psu_power(self, on: bool) -> dict:
        with self._gpio_lock:
            self._op_count += 1
            self._GPIO.output(CFG.PSU_ON, self._GPIO.LOW if on else self._GPIO.HIGH)
            if on:
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if self._GPIO.input(CFG.PWR_OK):
                        break
                    time.sleep(0.05)
            return self._psu_status_unlocked()

    def psu_status(self) -> dict:
        with self._gpio_lock:
            return self._psu_status_unlocked()

    def _psu_status_unlocked(self) -> dict:
        ps_on = self._GPIO.input(CFG.PSU_ON) == self._GPIO.LOW
        pwr_ok = bool(self._GPIO.input(CFG.PWR_OK))
        return {"ps_on": ps_on, "pwr_ok": pwr_ok}

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def health(self) -> dict:
        return {
            "uptime_s": time.time() - self._started_at,
            "op_count": self._op_count,
            "last_error": self._last_error,
        }
