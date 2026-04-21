"""Unit tests for the JSON-RPC dispatcher against the fake backend.
Runs anywhere — no hardware needed."""

import json

import pytest

from broker.fake_bus import FakeHardwareManager
from broker.rpc import build_method_table, handle_request
from tools import hw_config as CFG


@pytest.fixture()
def methods():
    return build_method_table(FakeHardwareManager())


def _call(methods, method, params=None, req_id=1):
    line = json.dumps({"id": req_id, "method": method, "params": params or {}})
    resp = handle_request(line, methods)
    assert resp is not None
    return json.loads(resp)


def test_health_reports_fake_backend(methods):
    r = _call(methods, "broker.health")
    assert r["id"] == 1
    assert r["result"]["backend"] == "fake"
    assert r["result"]["op_count"] == 0  # health itself doesn't tick


def test_dac_roundtrip(methods):
    _call(methods, "dac.set_voltage", {"idx": 0, "channel": 2, "volts": 1.5})
    r = _call(methods, "dac.get_voltage", {"idx": 0, "channel": 2})
    assert r["result"] == pytest.approx(1.5)


def test_adc_returns_list(methods):
    r = _call(methods, "adc.read_all", {"idx": 1})
    assert isinstance(r["result"], list)
    assert len(r["result"]) == 8


def test_ina_bad_address(methods):
    r = _call(methods, "ina.read", {"addr": 0x77})
    assert "error" in r
    assert r["error"]["code"] == "internal_error"


def test_tca_pin_set_and_read_back(methods):
    addr = CFG.TCA9555_ADDR_0
    _call(methods, "tca.write_pin", {"addr": addr, "port": 0, "pin": 3, "value": True})
    r = _call(methods, "tca.read", {"addr": addr})
    assert r["result"]["port0"] & (1 << 3)


def test_psu_power_cycle(methods):
    r = _call(methods, "psu.power", {"on": True})
    assert r["result"] == {"ps_on": True, "pwr_ok": True}
    r = _call(methods, "psu.power", {"on": False})
    assert r["result"] == {"ps_on": False, "pwr_ok": False}


def test_unknown_method(methods):
    r = _call(methods, "does.not.exist")
    assert r["error"]["code"] == "method_not_found"


def test_invalid_params(methods):
    r = _call(methods, "dac.set_voltage", {"idx": 0})  # missing channel and volts
    assert r["error"]["code"] == "invalid_params"


def test_parse_error(methods):
    resp = handle_request("not json at all", methods)
    assert resp is not None
    assert json.loads(resp)["error"]["code"] == "parse_error"


def test_notification_yields_no_response(methods):
    line = json.dumps({"method": "broker.health", "params": {}})  # no "id"
    assert handle_request(line, methods) is None


def test_op_counter_advances(methods):
    _call(methods, "adc.read", {"idx": 0, "channel": 0})
    _call(methods, "adc.read", {"idx": 0, "channel": 1})
    r = _call(methods, "broker.health")
    assert r["result"]["op_count"] == 2
