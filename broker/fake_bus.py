"""
Fake hardware backend for hil-broker unit tests and laptop dev.

Implements the HardwareBackend protocol with in-memory state. No SPI,
I2C, or GPIO access — safe to run anywhere. Values are plausible so
clients can exercise the full RPC surface end-to-end without caring
about the backend.
"""

from __future__ import annotations

import time

from tools import hw_config as CFG

_ADC_COUNT = 3
_ADC_CHANNELS = 8
_DAC_COUNT = 4
_DAC_CHANNELS = 4
_CAN_COUNT = 3

_VALID_INA_ADDRS = {
    CFG.INA226_ADDR_MLC1,
    CFG.INA226_ADDR_MLC2,
    CFG.INA226_ADDR_MLC3,
    CFG.INA226_ADDR_MLC4,
}
_VALID_TCA_ADDRS = {
    CFG.TCA9555_ADDR_0,
    CFG.TCA9555_ADDR_1,
    CFG.TCA9555_ADDR_2,
}


class FakeHardwareManager:
    def __init__(self) -> None:
        self._started_at = time.time()
        self._op_count = 0
        self._adc = [[0] * _ADC_CHANNELS for _ in range(_ADC_COUNT)]
        self._dac = [[0.0] * _DAC_CHANNELS for _ in range(_DAC_COUNT)]
        self._can_mode = [0x80] * _CAN_COUNT  # config mode
        self._can_tec = [0] * _CAN_COUNT
        self._can_rec = [0] * _CAN_COUNT
        self._tca_ports = {addr: {0: 0, 1: 0} for addr in _VALID_TCA_ADDRS}
        self._psu_on = False
        self._pwr_ok = False

    def _tick(self) -> None:
        self._op_count += 1

    # ADC
    def adc_read(self, idx: int, channel: int) -> int:
        self._tick()
        return self._adc[idx][channel]

    def adc_read_all(self, idx: int) -> list[int]:
        self._tick()
        return list(self._adc[idx])

    # DAC
    def dac_set_voltage(self, idx: int, channel: int, volts: float) -> None:
        self._tick()
        self._dac[idx][channel] = float(volts)

    def dac_get_voltage(self, idx: int, channel: int) -> float:
        self._tick()
        return self._dac[idx][channel]

    # CAN
    def can_set_mode(self, idx: int, mode: int) -> bool:
        self._tick()
        self._can_mode[idx] = mode
        return True

    def can_status(self, idx: int) -> dict:
        self._tick()
        return {"mode": self._can_mode[idx],
                "tec": self._can_tec[idx],
                "rec": self._can_rec[idx]}

    # INA226
    def ina_read(self, addr: int) -> dict:
        self._tick()
        if addr not in _VALID_INA_ADDRS:
            raise KeyError(f"no INA226 at 0x{addr:02X}")
        return {"bus_v": 0.0, "current": 0.5, "power": 0.0}

    # TCA9555
    def tca_read(self, addr: int) -> dict:
        self._tick()
        if addr not in _VALID_TCA_ADDRS:
            raise KeyError(f"no TCA9555 at 0x{addr:02X}")
        return {"port0": self._tca_ports[addr][0], "port1": self._tca_ports[addr][1]}

    def tca_write_pin(self, addr: int, port: int, pin: int, value: bool) -> None:
        self._tick()
        if addr not in _VALID_TCA_ADDRS:
            raise KeyError(f"no TCA9555 at 0x{addr:02X}")
        mask = 1 << pin
        cur = self._tca_ports[addr][port]
        self._tca_ports[addr][port] = (cur | mask) if value else (cur & ~mask)

    # PSU
    def psu_power(self, on: bool) -> dict:
        self._tick()
        self._psu_on = bool(on)
        self._pwr_ok = bool(on)
        return self.psu_status()

    def psu_status(self) -> dict:
        return {"ps_on": self._psu_on, "pwr_ok": self._pwr_ok}

    # Meta
    def health(self) -> dict:
        return {
            "uptime_s": time.time() - self._started_at,
            "op_count": self._op_count,
            "last_error": None,
            "backend": "fake",
        }
