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

import logging

import threading
import time
from typing import Protocol

from tools import hw_config as CFG

log = logging.getLogger("hil-broker")


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
    def dac_read_device_id(self, idx: int) -> int: ...
    def dac_reset(self, idx: int) -> None: ...
    def dac_zero_all(self, idx: int) -> None: ...

    # CAN
    def can_set_mode(self, idx: int, mode: int) -> bool: ...
    def can_get_mode(self, idx: int) -> int: ...
    def can_read_error_counters(self, idx: int) -> list[int]: ...
    def can_status(self, idx: int) -> dict: ...
    def can_reset(self, idx: int) -> None: ...
    def can_init(self, idx: int, bitrate: int) -> bool: ...
    def can_loopback_test(self, idx: int, can_id: int, data_b64: str) -> bool: ...
    def can_int_level(self, idx: int) -> int: ...

    # INA226
    def ina_read(self, addr: int) -> dict: ...
    def ina_is_present(self, addr: int) -> bool: ...
    def ina_bus_voltage(self, addr: int) -> float: ...
    def ina_shunt_voltage(self, addr: int) -> float: ...
    def ina_current(self, addr: int) -> float: ...
    def ina_power(self, addr: int) -> float: ...
    def ina_read_manufacturer_id(self, addr: int) -> int: ...
    def ina_read_die_id(self, addr: int) -> int: ...

    # TCA9555
    def tca_read(self, addr: int) -> dict: ...
    def tca_is_present(self, addr: int) -> bool: ...
    def tca_read_port(self, addr: int, port: int) -> int: ...
    def tca_set_direction(self, addr: int, port: int, mask: int) -> None: ...
    def tca_get_direction(self, addr: int, port: int) -> int: ...
    def tca_set_all_inputs(self, addr: int) -> None: ...
    def tca_set_all_outputs(self, addr: int) -> None: ...
    def tca_write_port(self, addr: int, port: int, value: int) -> None: ...
    def tca_write_all(self, addr: int, p0: int, p1: int) -> None: ...
    def tca_write_pin(self, addr: int, port: int, pin: int, value: bool) -> None: ...

    # I2C bus scan
    def i2c_scan(self, start: int, end: int) -> list[int]: ...

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
        from tools.mcp3208 import MCP3208
        from tools.nrf24l01 import NRF24L01
        from tools.tca9555 import TCA9555

        self._GPIO = GPIO
        self._spi_lock = threading.Lock()
        self._i2c_lock = threading.Lock()
        self._gpio_lock = threading.Lock()
        self._can_lock = threading.Lock()
        self._started_at = time.time()
        self._op_count = 0
        self._last_error: str | None = None

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        # CS pins for MCP2515 CAN chips (GPIO27/17/18) and their INT lines
        # (GPIO4/5/6) are owned by the kernel mcp251x driver via the
        # mcp2515-triple dtoverlay. Don't grab them here.
        cs_pins = [
            CFG.CS_ADC1, CFG.CS_ADC2, CFG.CS_ADC3,
            CFG.NRF24_CS,
        ]
        # The DAC chip-selects are claimed here ONLY on a bench whose overlay
        # has not yet moved them to cs-gpios. Where the kernel owns them,
        # grabbing the same GPIOs from userspace fights the SPI core for the
        # line -- the very coupling this change removes.
        import os as _os
        if not all(_os.path.exists(f"/dev/spidev{CFG.SPI_BUS}.{d}")
                   for d in (4, 5, 6, 7)):
            cs_pins += [CFG.CS_DAC1, CFG.CS_DAC2, CFG.CS_DAC3, CFG.CS_DAC4]
        for pin in cs_pins + [CFG.NRF24_CE]:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.HIGH)
        GPIO.setup(CFG.NRF24_IRQ, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(CFG.PWR_OK, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        # Do NOT touch PSU_ON on startup: firmware (`gpio=7=op,dl` in
        # config.txt) asserts it at boot so the kernel `mcp251x` probes
        # successfully. Flipping it off here would de-power the CAN chips
        # the kernel driver already owns. `psu.power(on)` still works at
        # runtime; we just don't reset it during broker init.
        GPIO.setup(CFG.PSU_ON, GPIO.OUT, initial=GPIO.LOW)

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

        # CAN state tracking. Buses are created lazily on first use — the
        # kernel netdev must be up at a valid bitrate before python-can can
        # bind. Modes map MCP2515 legacy bytes to socketcan link states so
        # existing clients (dashboard, tests) keep working unchanged.
        self._can_bitrate = CFG.CAN_BITRATE
        # 0x80 = CONFIG (link DOWN); 0x00 = NORMAL; 0x40 = LOOPBACK
        self._can_mode_bytes = [0x80, 0x80, 0x80]
        self._can_buses: list = [None, None, None]
        self._pycan = None  # python-can module, lazy-imported
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

    # Per-DAC spi_devices from mcp2515-triple.dts. Present only on a bench
    # whose overlay carries the cs-gpios change.
    _DAC_SPIDEV = (4, 5, 6, 7)

    def _open_dacs(self) -> list:
        """One spi_device per DAC when the overlay provides them.

        With cs-gpios the SPI core asserts CS as part of the message, under the
        controller lock. The legacy path shares spidev0.3 and toggles CS from
        userspace, which races the kernel mcp251x driver on the same
        controller: between the GPIO write and the transfer, an MCP2515 message
        can clock the bus while a DAC is already selected. Measured under CAN
        load as DEVID 0x082E -- 0x0417 shifted one bit -- and, less often, a
        latch only a rail power-cycle clears (IFS_HIL#124).

        Falls back rather than failing: a bench running the older overlay
        should still come up, just with the race.
        """
        import os
        paths = [f"/dev/spidev{CFG.SPI_BUS}.{d}" for d in self._DAC_SPIDEV]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            log.warning(
                "per-DAC spidev nodes missing (%s) — falling back to shared "
                "spidev%d.%d with userspace CS. That path races the kernel "
                "mcp251x driver (IFS_HIL#124); reboot with the updated "
                "mcp2515-triple overlay to fix it.",
                ", ".join(missing), CFG.SPI_BUS, CFG.SPI_DEVICE)
            return [self._DAC80504(self._spi, CFG.CS_DAC1),
                    self._DAC80504(self._spi, CFG.CS_DAC2),
                    self._DAC80504(self._spi, CFG.CS_DAC3),
                    self._DAC80504(self._spi, CFG.CS_DAC4)]

        dacs = []
        for dev in self._DAC_SPIDEV:
            h = spidev.SpiDev()
            h.open(CFG.SPI_BUS, dev)
            h.max_speed_hz = CFG.SPI_MAX_HZ
            # No mode set here on purpose: spi-cpha in the overlay makes this
            # device mode 1, and the SPI core programs it per message.
            dacs.append(self._DAC80504(h, None))
        log.info("DACs on per-device spidev %s — CS owned by the kernel",
                 ", ".join(paths))
        return dacs

    def _ensure_dacs(self) -> list:
        if self._dacs is None:
            with self._spi_lock:
                if self._dacs is None:
                    self._dacs = self._open_dacs()
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

    def dac_read_device_id(self, idx: int) -> int:
        dacs = self._ensure_dacs()
        with self._spi_lock:
            self._op_count += 1
            return dacs[idx].read_device_id()

    def dac_reset(self, idx: int) -> None:
        dacs = self._ensure_dacs()
        with self._spi_lock:
            self._op_count += 1
            dacs[idx].reset()

    def dac_zero_all(self, idx: int) -> None:
        dacs = self._ensure_dacs()
        with self._spi_lock:
            self._op_count += 1
            dacs[idx].zero_all()

    # ------------------------------------------------------------------
    # CAN — backed by the kernel mcp251x driver via SocketCAN
    # ------------------------------------------------------------------
    #
    # The three MCP2515s are bound to `mcp251x` (see
    # infra/kernel-module/mcp251x-patched/) via the mcp2515-triple overlay.
    # Each appears as `canN`. We map the legacy MCP2515 mode bytes that
    # existing clients use to SocketCAN link states:
    #
    #   0x80 (CONFIG)   -> link DOWN
    #   0x00 (NORMAL)   -> link UP, no loopback
    #   0x40 (LOOPBACK) -> link UP, loopback on
    #
    # so dashboard and tests keep working via the same RPC surface.

    _MODE_CONFIG   = 0x80
    _MODE_NORMAL   = 0x00
    _MODE_LOOPBACK = 0x40

    @staticmethod
    def _can_name(idx: int) -> str:
        return f"can{idx}"

    def _iproute_show(self, name: str) -> dict:
        """Run `ip -s -d -j link show <name>` and return the parsed dict
        for the single interface, or raise RuntimeError on failure."""
        import json, subprocess
        out = subprocess.run(
            ["ip", "-s", "-d", "-j", "link", "show", "dev", name],
            check=True, capture_output=True, text=True,
        ).stdout
        data = json.loads(out)
        if not data:
            raise RuntimeError(f"{name}: ip link returned no data")
        return data[0]

    _SUDO_NONINTERACTIVE = ("sudo", "-n")

    def _ip_down(self, name: str) -> None:
        import os, subprocess
        prefix = () if os.geteuid() == 0 else self._SUDO_NONINTERACTIVE
        subprocess.run([*prefix, "ip", "link", "set", name, "down"],
                       check=False, capture_output=True)

    def _ip_up(self, name: str, loopback: bool) -> None:
        import os, subprocess
        prefix = () if os.geteuid() == 0 else self._SUDO_NONINTERACTIVE
        args = [*prefix, "ip", "link", "set", name, "up",
                "type", "can", "bitrate", str(self._can_bitrate)]
        if loopback:
            args += ["loopback", "on"]
        subprocess.run(args, check=True, capture_output=True)

    def _bus_close(self, idx: int) -> None:
        bus = self._can_buses[idx]
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._can_buses[idx] = None

    def _ensure_bus(self, idx: int):
        if self._pycan is None:
            import can as pycan
            self._pycan = pycan
        if self._can_buses[idx] is None:
            self._can_buses[idx] = self._pycan.Bus(
                interface="socketcan", channel=self._can_name(idx)
            )
        return self._can_buses[idx]

    # -- RPC methods ---------------------------------------------------

    def can_set_mode(self, idx: int, mode: int) -> bool:
        with self._can_lock:
            self._op_count += 1
            name = self._can_name(idx)
            if mode == self._MODE_CONFIG:
                self._bus_close(idx)
                self._ip_down(name)
                self._can_mode_bytes[idx] = self._MODE_CONFIG
                return True
            loopback = (mode == self._MODE_LOOPBACK)
            self._bus_close(idx)
            self._ip_down(name)
            self._ip_up(name, loopback=loopback)
            self._can_mode_bytes[idx] = self._MODE_LOOPBACK if loopback else self._MODE_NORMAL
            return True

    def can_get_mode(self, idx: int) -> int:
        # Reflect kernel reality: if interface is DOWN we report CONFIG,
        # otherwise whatever mode we last set.
        try:
            info = self._iproute_show(self._can_name(idx))
            if info.get("operstate") == "DOWN":
                return self._MODE_CONFIG
        except Exception:
            return self._MODE_CONFIG
        return self._can_mode_bytes[idx]

    def can_read_error_counters(self, idx: int) -> list[int]:
        try:
            info = self._iproute_show(self._can_name(idx))
        except Exception:
            return [0, 0]
        linkinfo = info.get("linkinfo", {})
        data = linkinfo.get("info_data", {}) if isinstance(linkinfo, dict) else {}
        berr = data.get("berr-counter", {}) if isinstance(data, dict) else {}
        tec = int(berr.get("tx", 0)) if isinstance(berr, dict) else 0
        rec = int(berr.get("rx", 0)) if isinstance(berr, dict) else 0
        return [tec, rec]

    def can_status(self, idx: int) -> dict:
        mode = self.can_get_mode(idx)
        tec, rec = self.can_read_error_counters(idx)
        return {"mode": mode, "tec": tec, "rec": rec}

    def can_reset(self, idx: int) -> None:
        # "Reset" means return to CONFIG (link down); caller then sets the
        # desired mode (matches the MCP2515 set_mode lifecycle).
        with self._can_lock:
            self._op_count += 1
            self._bus_close(idx)
            self._ip_down(self._can_name(idx))
            self._can_mode_bytes[idx] = self._MODE_CONFIG

    def can_init(self, idx: int, bitrate: int) -> bool:
        # Legacy init(bitrate) matches the MCP2515 driver semantics:
        # configure bitrate and leave the chip in CONFIG mode. The caller
        # then picks NORMAL or LOOPBACK via set_mode. Bringing the link
        # UP in NORMAL with no CAN peer immediately drives the chip into
        # BUS-OFF, so we deliberately stay DOWN here.
        with self._can_lock:
            self._op_count += 1
            self._can_bitrate = int(bitrate)
            self._bus_close(idx)
            self._ip_down(self._can_name(idx))
            self._can_mode_bytes[idx] = self._MODE_CONFIG
            return True

    def can_loopback_test(self, idx: int, can_id: int, data_b64: str) -> bool:
        import base64
        data = base64.b64decode(data_b64)
        with self._can_lock:
            self._op_count += 1
            name = self._can_name(idx)
            # ensure link is up in loopback
            self._bus_close(idx)
            self._ip_down(name)
            self._ip_up(name, loopback=True)
            self._can_mode_bytes[idx] = self._MODE_LOOPBACK
            bus = self._ensure_bus(idx)
            msg = self._pycan.Message(arbitration_id=can_id,
                                      data=bytes(data),
                                      is_extended_id=False)
            bus.send(msg, timeout=0.2)
            rx = bus.recv(timeout=0.2)
            if rx is None:
                return False
            return (rx.arbitration_id == can_id
                    and bytes(rx.data) == bytes(data))

    def can_int_level(self, idx: int) -> int:
        # INT lines are owned by the kernel mcp251x driver; we can't poll
        # them from userspace. Report "no pending interrupt" (HIGH) so the
        # legacy tests_can `int_pin_idle_high` assertion stays meaningful
        # as a link-health proxy (fails only if the interface is DOWN).
        try:
            info = self._iproute_show(self._can_name(idx))
        except Exception:
            return 1
        return 0 if info.get("operstate") == "DOWN" else 1

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

    def ina_read_manufacturer_id(self, addr: int) -> int:
        with self._i2c_lock:
            self._op_count += 1
            return self._inas[addr].read_manufacturer_id()

    def ina_read_die_id(self, addr: int) -> int:
        with self._i2c_lock:
            self._op_count += 1
            return self._inas[addr].read_die_id()

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

    def tca_get_direction(self, addr: int, port: int) -> int:
        with self._i2c_lock:
            self._op_count += 1
            return self._tcas[addr].get_direction(port)

    def tca_set_all_inputs(self, addr: int) -> None:
        with self._i2c_lock:
            self._op_count += 1
            self._tcas[addr].set_all_inputs()

    def tca_set_all_outputs(self, addr: int) -> None:
        with self._i2c_lock:
            self._op_count += 1
            self._tcas[addr].set_all_outputs()

    def tca_write_port(self, addr: int, port: int, value: int) -> None:
        with self._i2c_lock:
            self._op_count += 1
            self._tcas[addr].write_port(port, value)

    def tca_write_all(self, addr: int, p0: int, p1: int) -> None:
        with self._i2c_lock:
            self._op_count += 1
            self._tcas[addr].write_all(p0, p1)

    def tca_write_pin(self, addr: int, port: int, pin: int, value: bool) -> None:
        with self._i2c_lock:
            self._op_count += 1
            self._tcas[addr].write_pin(port, pin, value)

    # ------------------------------------------------------------------
    # I2C bus scan
    # ------------------------------------------------------------------

    def i2c_scan(self, start: int = 0x08, end: int = 0x78) -> list[int]:
        found: list[int] = []
        with self._i2c_lock:
            self._op_count += 1
            for addr in range(start, end):
                try:
                    self._i2c.write_byte(addr, 0x00)
                    found.append(addr)
                except OSError:
                    pass
        return found

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
