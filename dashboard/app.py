#!/usr/bin/env python3
"""
BACKPLANE_HIL Observability Dashboard
Lightweight Flask web server — runs directly on the RPi.

Usage:
    python3 dashboard/app.py [--port 8080]

The server polls all hardware every 2 s in a background thread and
caches the result. The HTTP API just reads from that cache, so page
loads are always fast regardless of I2C/SPI latency.
"""

import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import RPi.GPIO as GPIO
import smbus2
import spidev
from flask import Flask, jsonify, send_from_directory

# Make tools/ importable from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import hw_config as CFG
from tools.dac80504 import DAC80504
from tools.ina226 import INA226
from tools.mcp2515 import MCP2515
from tools.mcp3208 import MCP3208
from tools.nrf24l01 import NRF24L01
from tools.tca9555 import TCA9555

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardware initialisation
# ---------------------------------------------------------------------------

def _init_hardware():
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

    # Assert PSU_ON and wait for PWR_OK
    GPIO.setup(CFG.PSU_ON, GPIO.OUT)
    GPIO.output(CFG.PSU_ON, GPIO.LOW)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if GPIO.input(CFG.PWR_OK):
            break
        time.sleep(0.05)
    if not GPIO.input(CFG.PWR_OK):
        log.warning("PWR_OK not asserted — PSU may be off")
    time.sleep(0.3)

    spi = spidev.SpiDev()
    spi.open(CFG.SPI_BUS, CFG.SPI_DEVICE)
    spi.max_speed_hz = CFG.SPI_MAX_HZ
    spi.mode = 0b00
    spi.no_cs = True
    spi.lsbfirst = False

    i2c = smbus2.SMBus(CFG.I2C_BUS)
    time.sleep(0.05)

    return spi, i2c


_spi, _i2c = _init_hardware()

_adcs = [MCP3208(_spi, CFG.CS_ADC1),
         MCP3208(_spi, CFG.CS_ADC2),
         MCP3208(_spi, CFG.CS_ADC3)]
_ADC_NAMES = ["ADC1 (U9)", "ADC2 (U10)", "ADC3 (U11)"]

_dacs = [DAC80504(_spi, CFG.CS_DAC1),
         DAC80504(_spi, CFG.CS_DAC2),
         DAC80504(_spi, CFG.CS_DAC3),
         DAC80504(_spi, CFG.CS_DAC4)]
_DAC_NAMES = ["DAC1 (U12)", "DAC2 (U13)", "DAC3 (U14)", "DAC4 (U15)"]

_cans = [MCP2515(_spi, CFG.CS_CAN1, CFG.MCP2515_OSC_HZ),
         MCP2515(_spi, CFG.CS_CAN2, CFG.MCP2515_OSC_HZ),
         MCP2515(_spi, CFG.CS_CAN3, CFG.MCP2515_OSC_HZ)]
_CAN_NAMES = ["CAN1 (U17)", "CAN2 (U19)", "CAN3 (U21)"]
_CAN_MODES = {0x00: "normal", 0x20: "sleep", 0x40: "loopback",
              0x60: "listenonly", 0x80: "config"}

_inas = [INA226(_i2c, CFG.INA226_ADDR_MLC1, CFG.INA226_SHUNT_OHM),
         INA226(_i2c, CFG.INA226_ADDR_MLC2, CFG.INA226_SHUNT_OHM),
         INA226(_i2c, CFG.INA226_ADDR_MLC3, CFG.INA226_SHUNT_OHM),
         INA226(_i2c, CFG.INA226_ADDR_MLC4, CFG.INA226_SHUNT_OHM)]
_INA_NAMES = ["MLC1", "MLC2", "MLC3", "MLC4"]

_tcas = [TCA9555(_i2c, CFG.TCA9555_ADDR_0),
         TCA9555(_i2c, CFG.TCA9555_ADDR_1),
         TCA9555(_i2c, CFG.TCA9555_ADDR_2)]
_TCA_NAMES = ["U3 (0x20)", "U6 (0x21)", "U8 (0x22)"]

_nrf = NRF24L01(_spi, CFG.NRF24_CS, CFG.NRF24_CE)

# ---------------------------------------------------------------------------
# Hardware poll — runs in background thread, result cached in _state
# ---------------------------------------------------------------------------

_state: dict = {}
_state_lock = threading.Lock()
_hw_lock = threading.Lock()   # serialises all SPI/I2C access


def _safe(fn, fallback=None):
    """Call fn(); return fallback on any exception."""
    try:
        return fn()
    except Exception as exc:
        log.debug("hw read error: %s", exc)
        return fallback


def _poll():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with _hw_lock:
        psu_ok = bool(GPIO.input(CFG.PWR_OK))

        can_data = []
        for name, dev in zip(_CAN_NAMES, _cans):
            try:
                mode_byte = dev.get_mode()
                mode_str = _CAN_MODES.get(mode_byte & 0xE0, f"0x{mode_byte:02X}")
                tec, rec = dev.read_error_counters()
                can_data.append({"name": name, "ok": True,
                                  "mode": mode_str, "tec": tec, "rec": rec})
            except Exception:
                can_data.append({"name": name, "ok": False,
                                  "mode": "error", "tec": None, "rec": None})

        adc_data = []
        for name, dev in zip(_ADC_NAMES, _adcs):
            try:
                volts = [round(dev.read_voltage(ch), 4) for ch in range(8)]
                adc_data.append({"name": name, "ok": True, "channels": volts})
            except Exception:
                adc_data.append({"name": name, "ok": False, "channels": []})

        dac_data = []
        for name, dev in zip(_DAC_NAMES, _dacs):
            try:
                volts = [round(dev.get_voltage(ch), 4) for ch in range(4)]
                dac_data.append({"name": name, "ok": True, "channels": volts})
            except Exception:
                dac_data.append({"name": name, "ok": False, "channels": []})

        power_data = []
        for name, dev in zip(_INA_NAMES, _inas):
            try:
                present = dev.is_present()
                if present:
                    power_data.append({
                        "name": name, "ok": True, "present": True,
                        "current_mA": round(dev.current() * 1000, 2),
                        "power_mW":   round(dev.power()   * 1000, 2),
                        "shunt_mV":   round(dev.shunt_voltage() * 1000, 3),
                    })
                else:
                    power_data.append({"name": name, "ok": False, "present": False})
            except Exception:
                power_data.append({"name": name, "ok": False, "present": False})

        io_data = []
        for name, dev in zip(_TCA_NAMES, _tcas):
            try:
                present = dev.is_present()
                if present:
                    p0 = dev.read_port(0)
                    p1 = dev.read_port(1)
                    io_data.append({"name": name, "ok": True, "present": True,
                                    "port0": p0, "port1": p1})
                else:
                    io_data.append({"name": name, "ok": False, "present": False})
            except Exception:
                io_data.append({"name": name, "ok": False, "present": False})

        nrf_present = _safe(_nrf.is_present, False)

    result = {
        "timestamp": now,
        "psu": {"ok": psu_ok},
        "can": can_data,
        "adc": adc_data,
        "dac": dac_data,
        "power": power_data,
        "io": io_data,
        "nrf24": {"present": nrf_present},
    }

    with _state_lock:
        _state.clear()
        _state.update(result)


def _poll_loop(interval: float = 2.0):
    while True:
        try:
            _poll()
        except Exception as exc:
            log.error("poll error: %s", exc)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

_here = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=_here, static_url_path="")


@app.route("/")
def index():
    return send_from_directory(_here, "index.html")


@app.route("/api/status")
def status():
    with _state_lock:
        return jsonify(dict(_state))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BACKPLANE_HIL dashboard")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Hardware poll interval in seconds")
    args = parser.parse_args()

    # Prime the cache before accepting requests
    log.info("Running initial hardware poll...")
    _poll()

    t = threading.Thread(target=_poll_loop, args=(args.poll_interval,),
                         daemon=True, name="hw-poller")
    t.start()

    log.info("Dashboard available at http://0.0.0.0:%d", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
