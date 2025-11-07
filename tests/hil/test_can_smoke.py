import os, time
import can

IFACE = os.getenv("CAN_IFACE", "can0")

def test_can_loopback_echo():
    # Este test asume can0 UP en loopback (lo activamos con la tarea VS Code de abajo)
    bus = can.interface.Bus(bustype="socketcan", channel=IFACE, receive_own_messages=True)
    msg = can.Message(arbitration_id=0x123, is_extended_id=False, data=bytes.fromhex("DE AD BE EF"))
    bus.send(msg)
    rx = bus.recv(1.0)
    assert rx is not None, "No se recibió eco en loopback"
    assert rx.arbitration_id == msg.arbitration_id
    assert bytes(rx.data) == bytes(msg.data)
