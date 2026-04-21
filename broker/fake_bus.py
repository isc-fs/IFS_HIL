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
        self._tca_dir = {addr: {0: 0xFF, 1: 0xFF} for addr in _VALID_TCA_ADDRS}  # default: all inputs
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

    def adc_read_voltage(self, idx: int, channel: int) -> float:
        self._tick()
        return self._adc[idx][channel] * 3.3 / 4095

    # DAC
    def dac_set_voltage(self, idx: int, channel: int, volts: float) -> None:
        self._tick()
        self._dac[idx][channel] = float(volts)

    def dac_get_voltage(self, idx: int, channel: int) -> float:
        self._tick()
        return self._dac[idx][channel]

    def dac_read_device_id(self, idx: int) -> int:
        self._tick()
        return 0x0417  # matches DAC80504 expected DEVID

    def dac_reset(self, idx: int) -> None:
        self._tick()
        self._dac[idx] = [0.0] * _DAC_CHANNELS

    def dac_zero_all(self, idx: int) -> None:
        self._tick()
        self._dac[idx] = [0.0] * _DAC_CHANNELS

    # CAN
    def can_set_mode(self, idx: int, mode: int) -> bool:
        self._tick()
        self._can_mode[idx] = mode
        return True

    def can_get_mode(self, idx: int) -> int:
        self._tick()
        return self._can_mode[idx]

    def can_read_error_counters(self, idx: int) -> list[int]:
        self._tick()
        return [self._can_tec[idx], self._can_rec[idx]]

    def can_status(self, idx: int) -> dict:
        self._tick()
        return {"mode": self._can_mode[idx],
                "tec": self._can_tec[idx],
                "rec": self._can_rec[idx]}

    def can_reset(self, idx: int) -> None:
        self._tick()
        self._can_mode[idx] = 0x80  # config
        self._can_tec[idx] = 0
        self._can_rec[idx] = 0

    def can_init(self, idx: int, bitrate: int) -> bool:
        self._tick()
        self._can_mode[idx] = 0x80  # config after init
        return True

    def can_loopback_test(self, idx: int, can_id: int, data_b64: str) -> bool:
        self._tick()
        return True

    def can_int_level(self, idx: int) -> int:
        self._tick()
        return 1  # HIGH = no pending interrupt

    # INA226
    def _check_ina(self, addr: int) -> None:
        if addr not in _VALID_INA_ADDRS:
            raise KeyError(f"no INA226 at 0x{addr:02X}")

    def ina_read(self, addr: int) -> dict:
        self._tick(); self._check_ina(addr)
        return {"bus_voltage_V": 0.0, "shunt_voltage_V": 0.005,
                "current_A": 0.5, "power_W": 0.0}

    def ina_is_present(self, addr: int) -> bool:
        self._tick(); self._check_ina(addr)
        return True

    def ina_bus_voltage(self, addr: int) -> float:
        self._tick(); self._check_ina(addr)
        return 0.0

    def ina_shunt_voltage(self, addr: int) -> float:
        self._tick(); self._check_ina(addr)
        return 0.005

    def ina_current(self, addr: int) -> float:
        self._tick(); self._check_ina(addr)
        return 0.5

    def ina_power(self, addr: int) -> float:
        self._tick(); self._check_ina(addr)
        return 0.0

    def ina_read_manufacturer_id(self, addr: int) -> int:
        self._tick(); self._check_ina(addr)
        return 0x5449

    def ina_read_die_id(self, addr: int) -> int:
        self._tick(); self._check_ina(addr)
        return 0x2260

    # TCA9555
    def _check_tca(self, addr: int) -> None:
        if addr not in _VALID_TCA_ADDRS:
            raise KeyError(f"no TCA9555 at 0x{addr:02X}")

    def tca_read(self, addr: int) -> dict:
        self._tick(); self._check_tca(addr)
        return {"input_port0":  self._tca_ports[addr][0],
                "input_port1":  self._tca_ports[addr][1],
                "output_port0": self._tca_ports[addr][0],
                "output_port1": self._tca_ports[addr][1],
                "config_port0": 0, "config_port1": 0}

    def tca_is_present(self, addr: int) -> bool:
        self._tick(); self._check_tca(addr)
        return True

    def tca_read_port(self, addr: int, port: int) -> int:
        self._tick(); self._check_tca(addr)
        return self._tca_ports[addr][port]

    def tca_set_direction(self, addr: int, port: int, mask: int) -> None:
        self._tick(); self._check_tca(addr)
        self._tca_dir[addr][port] = mask & 0xFF

    def tca_get_direction(self, addr: int, port: int) -> int:
        self._tick(); self._check_tca(addr)
        return self._tca_dir[addr][port]

    def tca_set_all_inputs(self, addr: int) -> None:
        self._tick(); self._check_tca(addr)
        self._tca_dir[addr] = {0: 0xFF, 1: 0xFF}

    def tca_set_all_outputs(self, addr: int) -> None:
        self._tick(); self._check_tca(addr)
        self._tca_dir[addr] = {0: 0x00, 1: 0x00}

    def tca_write_port(self, addr: int, port: int, value: int) -> None:
        self._tick(); self._check_tca(addr)
        self._tca_ports[addr][port] = value & 0xFF

    def tca_write_all(self, addr: int, p0: int, p1: int) -> None:
        self._tick(); self._check_tca(addr)
        self._tca_ports[addr] = {0: p0 & 0xFF, 1: p1 & 0xFF}

    def tca_write_pin(self, addr: int, port: int, pin: int, value: bool) -> None:
        self._tick(); self._check_tca(addr)
        mask = 1 << pin
        cur = self._tca_ports[addr][port]
        self._tca_ports[addr][port] = (cur | mask) if value else (cur & ~mask)

    # I2C bus scan
    def i2c_scan(self, start: int = 0x08, end: int = 0x78) -> list[int]:
        self._tick()
        present = sorted(_VALID_INA_ADDRS | _VALID_TCA_ADDRS)
        return [a for a in present if start <= a < end]

    # nRF24L01+
    def nrf_is_present(self) -> bool:
        self._tick()
        return False  # matches bench reality: not populated

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
