#!/usr/bin/env python3
"""
BACKPLANE_HIL Observability Dashboard
Lightweight Flask web server — runs directly on the RPi.

Usage:
    python3 dashboard/app.py [--port 8080]

As of Phase 2 of the broker migration, the dashboard does NOT open any
/dev/spidev*, /dev/i2c-1, or /dev/gpiochip0 itself. Every hardware
access goes through hil-broker over its Unix socket. Start the broker
(systemctl start hil-broker) before launching the dashboard.

The server polls all hardware every 2 s in a background thread and
caches the result. The HTTP API just reads from that cache.
"""

import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import json
import queue

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

# Make tools/ and broker/ importable from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# CAN trace tab backend -- standalone, doesn't touch the broker (AF_CAN sockets
# are not /dev/* devices so the broker monopoly invariant doesn't apply).
from dashboard.can_tracer import CanTracer  # noqa: E402

_can_tracer: CanTracer | None = None

from tools import hw_config as CFG
from tools.hil_client import (
    DAC80504, INA226, MCP2515, MCP3208, NRF24L01, TCA9555,
    psu_power, psu_status,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Driver proxies (no hardware opened — broker owns the buses)
# ---------------------------------------------------------------------------

_adcs = [MCP3208(idx=0), MCP3208(idx=1), MCP3208(idx=2)]
_ADC_NAMES = ["ADC1 (U9)", "ADC2 (U10)", "ADC3 (U11)"]

_dacs = [DAC80504(idx=0), DAC80504(idx=1), DAC80504(idx=2), DAC80504(idx=3)]
_DAC_NAMES = ["DAC1 (U12)", "DAC2 (U13)", "DAC3 (U14)", "DAC4 (U15)"]

_cans = [MCP2515(idx=0), MCP2515(idx=1), MCP2515(idx=2)]
_CAN_NAMES = ["CAN1 (U17)", "CAN2 (U19)", "CAN3 (U21)"]
_CAN_MODES = {0x00: "normal", 0x20: "sleep", 0x40: "loopback",
              0x60: "listenonly", 0x80: "config"}

_inas = [INA226(addr=CFG.INA226_ADDR_MLC1),
         INA226(addr=CFG.INA226_ADDR_MLC2),
         INA226(addr=CFG.INA226_ADDR_MLC3),
         INA226(addr=CFG.INA226_ADDR_MLC4)]
_INA_NAMES = ["MLC1", "MLC2", "MLC3", "MLC4"]

_tcas = [TCA9555(addr=CFG.TCA9555_ADDR_0),
         TCA9555(addr=CFG.TCA9555_ADDR_1),
         TCA9555(addr=CFG.TCA9555_ADDR_2)]
_TCA_NAMES = ["U3 (0x20)", "U6 (0x21)", "U8 (0x22)"]

_nrf = NRF24L01()

_TCA_BY_ADDR = {
    CFG.TCA9555_ADDR_0: _tcas[0],
    CFG.TCA9555_ADDR_1: _tcas[1],
    CFG.TCA9555_ADDR_2: _tcas[2],
}

_CARRIER_RELAYS = {
    1: CFG.RELAY_PINS["K1"],
    2: CFG.RELAY_PINS["K2"],
    3: CFG.RELAY_PINS["K3"],
    4: CFG.RELAY_PINS["K4"],
}

_relay_state: dict = {slot: False for slot in _CARRIER_RELAYS}
_psu_on: bool = False


def _init_relays():
    """Configure relay output pins and ensure all carriers start powered off."""
    tca = _TCA_BY_ADDR[CFG.TCA9555_ADDR_0]
    tca.set_direction(0, 0x00)   # port 0 all outputs
    tca.write_port(0, 0x00)      # all relay coils de-energised


# ---------------------------------------------------------------------------
# Hardware poll — runs in background thread, result cached in _state
# ---------------------------------------------------------------------------

_state: dict = {}
_state_lock = threading.Lock()


def _safe(fn, fallback=None):
    try:
        return fn()
    except Exception as exc:
        log.debug("hw read error: %s", exc)
        return fallback


def _poll():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    psu_ok = _safe(lambda: psu_status()["pwr_ok"], False)

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
    for i, (name, dev) in enumerate(zip(_INA_NAMES, _inas)):
        slot = i + 1
        try:
            present = dev.is_present()
            if present:
                current_mA = round(dev.current() * 1000, 2)
                power_data.append({
                    "name": name, "ok": True, "present": True,
                    "current_mA": current_mA,
                    "power_mW":   round(dev.power()   * 1000, 2),
                    "shunt_mV":   round(dev.shunt_voltage() * 1000, 3),
                    "relay_on":   _relay_state[slot],
                    "overcurrent": abs(current_mA) / 1000 > CFG.MLC_CURRENT_MAX_A,
                })
            else:
                power_data.append({"name": name, "ok": False, "present": False,
                                   "relay_on": _relay_state[slot], "overcurrent": False})
        except Exception:
            power_data.append({"name": name, "ok": False, "present": False,
                               "relay_on": _relay_state[slot], "overcurrent": False})

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
        "psu": {"ok": psu_ok, "on": _psu_on},
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


@app.route("/api/carrier/<int:slot>/power", methods=["POST"])
def carrier_power(slot):
    if slot not in _CARRIER_RELAYS:
        return jsonify({"error": "invalid slot, use 1–4"}), 400
    data = request.get_json(force=True, silent=True) or {}
    on = bool(data.get("on", False))
    addr, port, pin = _CARRIER_RELAYS[slot]
    try:
        tca = _TCA_BY_ADDR[addr]
        tca.set_direction(port, 0x00)
        tca.write_pin(port, pin, on)
        _relay_state[slot] = on
        log.info("Carrier MLC%d power %s", slot, "ON" if on else "OFF")
        return jsonify({"slot": slot, "on": on})
    except Exception as exc:
        log.error("relay error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/psu/power", methods=["POST"])
def psu_power_endpoint():
    global _psu_on
    data = request.get_json(force=True, silent=True) or {}
    on = bool(data.get("on", False))
    try:
        result = psu_power(on)
        if on and not result.get("pwr_ok"):
            return jsonify({"error": "PWR_OK not asserted after 5 s"}), 500
        if not on:
            for slot in _relay_state:
                _relay_state[slot] = False
        _psu_on = on
        log.info("PSU power %s", "ON" if on else "OFF")
        return jsonify({"on": on})
    except Exception as exc:
        log.error("PSU power error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/dac/<int:idx>/channel/<int:ch>", methods=["POST"])
def dac_set(idx, ch):
    if not (0 <= idx < len(_dacs)):
        return jsonify({"error": f"invalid DAC index, use 0–{len(_dacs) - 1}"}), 400
    if not (0 <= ch <= 3):
        return jsonify({"error": "invalid channel, use 0–3"}), 400
    data = request.get_json(force=True, silent=True) or {}
    try:
        voltage = float(data["voltage"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "missing or invalid 'voltage' field"}), 400
    try:
        _dacs[idx].set_voltage(ch, voltage)
        log.info("DAC%d ch%d → %.4f V", idx, ch, voltage)
        return jsonify({"idx": idx, "channel": ch, "voltage": voltage})
    except Exception as exc:
        log.error("DAC set error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/tca/<int:addr>/port/<int:port>/pin/<int:pin>", methods=["POST"])
def tca_write_pin(addr, port, pin):
    if addr not in _TCA_BY_ADDR:
        return jsonify({"error": f"unknown TCA address 0x{addr:02x}"}), 400
    if port not in (0, 1) or not (0 <= pin <= 7):
        return jsonify({"error": "port must be 0 or 1, pin must be 0–7"}), 400
    data = request.get_json(force=True, silent=True) or {}
    value = bool(data.get("value", False))
    try:
        tca = _TCA_BY_ADDR[addr]
        tca.set_direction(port, 0x00)
        tca.write_pin(port, pin, value)
        log.info("TCA 0x%02x port%d pin%d → %d", addr, port, pin, value)
        return jsonify({"addr": addr, "port": port, "pin": pin, "value": value})
    except Exception as exc:
        log.error("TCA write error: %s", exc)
        return jsonify({"error": str(exc)}), 500


_MODE_NAME_TO_BYTE = {
    "normal": 0x00, "sleep": 0x20, "loopback": 0x40,
    "listenonly": 0x60, "config": 0x80,
}


@app.route("/api/can/<int:idx>/mode", methods=["POST"])
def can_set_mode(idx):
    if not (0 <= idx < len(_cans)):
        return jsonify({"error": f"invalid CAN index, use 0–{len(_cans) - 1}"}), 400
    data = request.get_json(force=True, silent=True) or {}
    mode_str = data.get("mode", "")
    if mode_str not in _MODE_NAME_TO_BYTE:
        return jsonify({"error": f"invalid mode, use: {list(_MODE_NAME_TO_BYTE)}"}), 400
    try:
        ok = _cans[idx].set_mode(_MODE_NAME_TO_BYTE[mode_str])
        if not ok:
            return jsonify({"error": "mode change timed out"}), 500
        log.info("CAN%d mode → %s", idx, mode_str)
        return jsonify({"idx": idx, "mode": mode_str})
    except Exception as exc:
        log.error("CAN mode error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# CAN trace tab
# ---------------------------------------------------------------------------

@app.route("/api/can/recent")
def can_recent():
    """REST snapshot of the in-memory ring buffer."""
    if _can_tracer is None:
        return jsonify({"error": "CAN tracer not started"}), 503
    bus = request.args.get("bus") or None
    try:
        n = max(1, min(int(request.args.get("n", 200)), 2000))
    except ValueError:
        n = 200
    return jsonify({"frames": _can_tracer.recent(bus=bus, n=n)})


@app.route("/api/can/stream")
def can_stream():
    """Server-Sent Events feed: every frame on any bus, as it arrives.

    Browser side uses `new EventSource('/api/can/stream')`. Each event's
    `data` is one JSON-encoded frame object."""
    if _can_tracer is None:
        return jsonify({"error": "CAN tracer not started"}), 503

    sub = _can_tracer.subscribe(maxsize=512)

    @stream_with_context
    def gen():
        # Send a comment line every 15 s so proxies / EventSource don't time out.
        last_keepalive = time.monotonic()
        try:
            yield ": tracer-connected\n\n"
            while True:
                try:
                    frame = sub.get(timeout=5.0)
                    yield f"data: {json.dumps(frame)}\n\n"
                except queue.Empty:
                    pass
                if time.monotonic() - last_keepalive > 15.0:
                    yield ": keepalive\n\n"
                    last_keepalive = time.monotonic()
        except GeneratorExit:
            pass
        finally:
            _can_tracer.unsubscribe(sub)

    headers = {
        "Content-Type":  "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # disable nginx buffering if proxied
    }
    return Response(gen(), headers=headers)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BACKPLANE_HIL dashboard")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Hardware poll interval in seconds")
    args = parser.parse_args()

    # Reflect broker's current PSU state
    try:
        _psu_on = psu_status()["ps_on"]
    except Exception as exc:
        log.warning("could not read initial PSU state from broker: %s", exc)

    _init_relays()

    # Bring the CAN tracer online -- skips interfaces it can't bind to (e.g.
    # off-bench Mac dev) so the dashboard still starts cleanly.
    _can_tracer = CanTracer()
    _can_tracer.start()
    globals()["_can_tracer"] = _can_tracer

    log.info("Running initial hardware poll...")
    _poll()

    t = threading.Thread(target=_poll_loop, args=(args.poll_interval,),
                         daemon=True, name="hw-poller")
    t.start()

    log.info("Dashboard available at http://0.0.0.0:%d", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
