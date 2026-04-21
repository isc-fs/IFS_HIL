"""End-to-end socket test: start the server with a fake backend,
connect with BrokerClient, exercise a few methods."""

import os
import tempfile
import threading
import time

import pytest

from broker.fake_bus import FakeHardwareManager
from broker.server import BrokerClient, serve


@pytest.fixture()
def broker_socket():
    backend = FakeHardwareManager()
    tmp = tempfile.mkdtemp(prefix="hil-broker-")
    sock_path = os.path.join(tmp, "broker.sock")

    t = threading.Thread(target=serve, args=(backend, sock_path), daemon=True)
    t.start()

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if os.path.exists(sock_path):
            break
        time.sleep(0.02)
    assert os.path.exists(sock_path), "broker never bound the socket"

    yield sock_path

    # Daemon thread dies with the process; nothing else to do.


def test_end_to_end_roundtrip(broker_socket):
    with BrokerClient(broker_socket) as c:
        assert c.call("broker.health")["backend"] == "fake"
        c.call("dac.set_voltage", idx=0, channel=0, volts=2.2)
        assert c.call("dac.get_voltage", idx=0, channel=0) == pytest.approx(2.2)


def test_multiple_clients_serialise(broker_socket):
    # Two clients writing to different DACs should both succeed without
    # stepping on each other.
    c1 = BrokerClient(broker_socket)
    c2 = BrokerClient(broker_socket)
    try:
        c1.call("dac.set_voltage", idx=0, channel=0, volts=1.0)
        c2.call("dac.set_voltage", idx=1, channel=0, volts=2.0)
        assert c1.call("dac.get_voltage", idx=0, channel=0) == pytest.approx(1.0)
        assert c2.call("dac.get_voltage", idx=1, channel=0) == pytest.approx(2.0)
    finally:
        c1.close()
        c2.close()


def test_error_surface(broker_socket):
    with BrokerClient(broker_socket) as c:
        with pytest.raises(RuntimeError, match="method_not_found"):
            c.call("not.a.method")
