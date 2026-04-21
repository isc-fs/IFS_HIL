"""
JSON-RPC dispatcher for the hil-broker.

Wire format is line-delimited JSON (one request per line, one response per
line). Requests:

    {"id": <any>, "method": "adc.read", "params": {"idx": 0, "channel": 3}}

Responses (success):

    {"id": <any>, "result": <json-value>}

Responses (error):

    {"id": <any>, "error": {"code": "...", "message": "..."}}

Requests with no "id" are treated as notifications — no response is sent.
This module is transport-agnostic; broker/server.py wraps it with a Unix
socket.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from broker.bus import HardwareBackend

log = logging.getLogger(__name__)


class RpcError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def build_method_table(backend: HardwareBackend) -> dict[str, Callable[..., Any]]:
    """Return the name→callable map that handle_request looks up."""
    return {
        "adc.read": backend.adc_read,
        "adc.read_all": backend.adc_read_all,
        "dac.set_voltage": backend.dac_set_voltage,
        "dac.get_voltage": backend.dac_get_voltage,
        "can.set_mode": backend.can_set_mode,
        "can.status": backend.can_status,
        "ina.read": backend.ina_read,
        "tca.read": backend.tca_read,
        "tca.write_pin": backend.tca_write_pin,
        "psu.power": backend.psu_power,
        "psu.status": backend.psu_status,
        "broker.health": backend.health,
    }


def handle_request(
    line: str, methods: dict[str, Callable[..., Any]]
) -> str | None:
    """Parse one JSON line, dispatch, return the response line (with \\n)
    or None if the request was a notification."""
    try:
        req = json.loads(line)
    except json.JSONDecodeError as e:
        return _error_line(None, "parse_error", f"invalid JSON: {e}")

    if not isinstance(req, dict):
        return _error_line(None, "invalid_request", "request must be a JSON object")

    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if not isinstance(method, str):
        return _error_line(req_id, "invalid_request", "missing or non-string 'method'")
    if not isinstance(params, dict):
        return _error_line(req_id, "invalid_request", "'params' must be an object")

    fn = methods.get(method)
    if fn is None:
        return _error_line(req_id, "method_not_found", f"unknown method: {method}")

    try:
        result = fn(**params)
    except RpcError as e:
        return _error_line(req_id, e.code, e.message)
    except TypeError as e:
        return _error_line(req_id, "invalid_params", str(e))
    except Exception as e:  # noqa: BLE001 — surface every driver failure
        log.exception("method %s raised", method)
        return _error_line(req_id, "internal_error", f"{type(e).__name__}: {e}")

    if req_id is None:
        return None
    return json.dumps({"id": req_id, "result": result}, default=_json_default) + "\n"


def _error_line(req_id: Any, code: str, message: str) -> str:
    return json.dumps({"id": req_id, "error": {"code": code, "message": message}}) + "\n"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, bytes):
        import base64
        return base64.b64encode(obj).decode("ascii")
    raise TypeError(f"{type(obj).__name__} not JSON-serialisable")
