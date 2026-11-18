"""
Block F (real AMS) — ECU <-> live AMS integration (IFS08-CE-ECU#94).

Runs with the REAL AMS powered on MLC2 (not the acu_inject sim), so the ECU gates
on the actual 0x020/0x12C/0x4A0 the AMS puts on the shared ACU bus (can2).

  F-005 the live AMS emits the frames the ECU consumes (0x020, 0x12C, 0x4A0)
  F-001 the ECU's ok_precharge tracks the AMS's 0x020 (the real precharge gate)
  F-004 AMS-stale fail-safe: cut AMS power -> ECU AMS-fresh lapses -> ok_precharge false

F-002 (error inhibit) / F-003 (low-cell derate) stay on the acu_inject sim
(test_block_f_ams.py) -- they need injected AMS states that would fight the live AMS.
"""
from __future__ import annotations

import os
import time

import pytest

from tools.firmware_test.vcu import can_map as M

AMS_RELAY_PIN = 1   # K2 = TCA 0x20 port0 pin1 = MLC2 (AMS)
PRECHARGE = 1       # CtrlState::Precharge


def _broker():
    from broker.server import BrokerClient
    return BrokerClient(os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))


@pytest.fixture
def real_ams(mlc_powered):
    """Power the real AMS on MLC2 alongside the ECU (MLC4). Skips if the AMS isn't
    drawing boot current (not seated)."""
    c = _broker()
    c.call("tca.set_direction", addr=0x20, port=0, mask=0x00)
    c.call("tca.write_pin", addr=0x20, port=0, pin=AMS_RELAY_PIN, value=True)
    time.sleep(2.5)
    mA = c.call("ina.current", addr=0x41) * 1000.0
    c.close()
    if mA < 50:
        pytest.skip(f"AMS not drawing current on MLC2 ({mA:.0f} mA) -- not seated?")
    return {"current_mA": mA}


def _wait_fsm(pit_diag, pred, timeout_s):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = pit_diag["read_fsm"](0.3)
        if last is not None and pred(last):
            return last
        time.sleep(0.05)
    return last


def _ok_precharge(observe_acu):
    f = observe_acu.last(M.ID_PIT_STATUS, extended=False)
    return bool(M.decode_pit_status(f.data).get("flags", 0) & M.PIT_FLAG_OK_PRECHARGE) if f else None


class TestF005AmsEmitsConsumedFrames:
    def test_f005_ams_emits_consumed_frames(self, real_ams, fresh_boot, observe_acu):
        """F-005: the live AMS emits 0x020 / 0x12C / 0x4A0 -- the frames the ECU gates
        on for precharge, derate, and the error latch (an earlier bench saw only
        0x4A2/0x4A4)."""
        time.sleep(2.0)
        for fid, name in [(0x020, "precharge"), (0x12C, "v_cell_min"), (0x4A0, "AMS_status")]:
            assert observe_acu.last(fid, extended=False) is not None, \
                f"live AMS not emitting 0x{fid:03X} ({name})"


class TestF001RealPrechargeGate:
    def test_f001_ok_precharge_tracks_real_ams(self, real_ams, fresh_boot, inv_heartbeat,
                                               pit_diag, observe_acu):
        """F-001: past the vdc gate, the ECU's ok_precharge mirrors the live AMS's
        0x020[0] -- it holds at Precharge while the AMS reports precharge-not-done."""
        inv_heartbeat["vdc_ready"]()
        s = _wait_fsm(pit_diag, lambda s: s >= PRECHARGE, 4.0)
        if s is None or s < PRECHARGE:
            pytest.skip(f"ECU didn't leave WaitInvVdcConfig (got {s}) -- inverter vdc?")
        time.sleep(0.5)
        f020 = observe_acu.last(0x020, extended=False)
        assert f020 is not None, "no 0x020 from the live AMS"
        ams_says_ok = f020.data[0] != 0
        assert _ok_precharge(observe_acu) == ams_says_ok, \
            f"ECU ok_precharge {_ok_precharge(observe_acu)} != live AMS 0x020[0]={f020.data[0]}"


class TestF004AmsStaleFailsafe:
    def test_f004_ams_stale_drops_ok_precharge(self, real_ams, fresh_boot, inv_heartbeat,
                                               pit_diag, observe_acu):
        """F-004: cut AMS power -> within the 200 ms AMS-stale window the ECU's
        AMS-fresh lapses and ok_precharge reads false (a stale AMS = not-ok, the
        re-arm path, NOT an error latch)."""
        inv_heartbeat["vdc_ready"]()
        if _wait_fsm(pit_diag, lambda s: s >= PRECHARGE, 4.0) is None:
            pytest.skip("ECU didn't reach Precharge")
        c = _broker()
        try:
            c.call("tca.write_pin", addr=0x20, port=0, pin=AMS_RELAY_PIN, value=False)  # AMS off
            time.sleep(0.6)   # > the 200 ms AMS-stale window
            assert _ok_precharge(observe_acu) is False, \
                "ECU still ok_precharge after the AMS went stale"
        finally:
            c.call("tca.write_pin", addr=0x20, port=0, pin=AMS_RELAY_PIN, value=True)   # restore
            c.close()


class TestF006RealAmsFaultInhibitsEcu:
    def test_f006_real_ams_fault_inhibits_ecu(self, real_ams, fresh_boot, inv_heartbeat,
                                              pit_diag, observe_acu, observe_inv):
        """F-006: closed-loop AMS + ECU fault propagation — inject a real cell
        overvoltage on the Pico LTC emulator -> live AMS (MLC2) trips to Error
        (0x4A0[0]=5) -> ECU (MLC4) enters AmsError and commands zero torque."""
        from tools.pico_ltc_emulator.host.pico_ltc_client import PicoLtcClient
        try:
            pico = PicoLtcClient()
            pico.open()
            pico.ping()
        except Exception as e:
            pytest.skip(f"Pico LTC emulator unavailable for closed-loop fault: {e}")

        try:
            pico.set_all_cells(3600)
            pico.set_all_temps(25.0)
            # Wait past AMS boot grace + first poll + debounce
            time.sleep(3.5)

            # Inject cell overvoltage (4250 mV > 4200 mV) into the live AMS
            pico.set_all_cells(4250)
            deadline = time.monotonic() + 5.0
            ams_err = False
            while time.monotonic() < deadline:
                f0 = observe_acu.last(0x4A0, extended=False)
                if f0 is not None and f0.data[0] == 5:
                    ams_err = True
                    break
                time.sleep(0.05)
            assert ams_err, "live AMS did not trip to Error (0x4A0[0]=5) on 4250 mV OV"

            # ECU must enter AmsError (6) and command zero torque
            s = _wait_fsm(pit_diag, lambda v: v == int(M.VcuFsmState.AMS_ERROR), 3.0)
            assert s == int(M.VcuFsmState.AMS_ERROR), \
                f"ECU FSM={M.VcuFsmState.name_of(s)} instead of AmsError after live AMS fault"
            f_t = observe_inv.last(M.ID_INV_TORQUE, extended=False)
            if f_t is not None:
                assert abs(M.decode_inv_torque(f_t.data)["torque_nm"]) <= 2
        finally:
            try:
                pico.set_all_cells(3600)
                pico.close()
            except Exception:
                pass
