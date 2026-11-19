"""
Block J — Soak & flash endurance (IFS08-CE-ECU#71), modest bench variant.

J-001 holds the ECU in Active for VCU_SOAK_S (default 60 s) under a varying APPS
load: the 0x100 heartbeat stays alive, the FSM stays Active, and 0x704 uptime
climbs monotonically (a reset would drop it / change reset_cause). J-002 re-
flashes the running stub image over the BL VCU_REFLASH_N times (default 2) and
verifies every cycle boots clean and leaves the BL reachable.

Needs ECU_HIL_STUB_START_BTN + start_btn_via_can + a live DAC (J-001), and the
stub .bin on the Pi at /tmp/ECU08_hil.bin (J-002, override ECU_HIL_STUB_BIN).
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from tools.firmware_test.vcu import can_map as M
from tools.firmware_test.vcu.can_map import VcuFsmState as S

SOAK_S = float(os.environ.get("VCU_SOAK_S", "60"))
STUB_BIN = os.environ.get("ECU_HIL_STUB_BIN", "/tmp/ECU08_hil.bin")
_BL_SP = 0.875


def _wait_fsm(pit_diag, pred, timeout_s):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = pit_diag["read_fsm"](0.3)
        if last is not None and pred(last):
            return last
        time.sleep(0.05)
    return last


def _apps(pedals, vcu_profile, p):
    for which in ("apps1", "apps2"):
        lo = int(vcu_profile[f"{which}_adc_min"])
        hi = int(vcu_profile[f"{which}_adc_max"])
        pedals[f"set_{which}"](int(lo + p / 100.0 * (hi - lo)))


def _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag, vcu_profile):
    inv_heartbeat["vdc_ready"]()
    acu_inject["set_precharge"](1)
    if _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 4.0) is None:
        pytest.skip("FSM never reached WAIT_START_BRAKE -- DAC/gates")
    pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
    start_button["press"]()
    inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
    s = _wait_fsm(pit_diag, lambda s: s == S.ACTIVE,
                  float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0)
    if s == S.ACTIVE:
        pedals["set_brake"](0)
    return s


def _health(observe_acu):
    f = observe_acu.last(M.ID_PIT_HEALTH, extended=False)
    return M.decode_pit_health(f.data) if f is not None else None


class TestJ001DriveSoak:
    def test_j001_drive_soak(self, fresh_boot, inv_heartbeat, acu_inject, pedals,
                             start_button, pit_diag, observe_acu, vcu_profile):
        """J-001: VCU_SOAK_S in Active under a varying APPS load -> heartbeat alive,
        FSM stays Active, 0x704 uptime climbs (no reset)."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- DAC/gates")
        h0 = _health(observe_acu)
        if h0 is None:
            pytest.skip("no 0x704 health frame")
        rc0, up_prev = h0["reset_cause"], h0["uptime_s"]
        deadline = time.monotonic() + SOAK_S
        samples = 0
        while time.monotonic() < deadline:
            _apps(pedals, vcu_profile, 30 + (samples * 7) % 40)   # vary 30..70%
            time.sleep(1.0)
            samples += 1
            assert observe_acu.last(M.ID_HEARTBEAT, extended=False) is not None, \
                "0x100 heartbeat missing during soak"
            fsm = pit_diag["read_fsm"](0.3)
            assert fsm == S.ACTIVE, f"FSM left Active ({S.name_of(fsm)}) -- reset during soak?"
            h = _health(observe_acu)
            assert h["reset_cause"] == rc0, "0x704 reset_cause changed -> a reset occurred"
            # uptime_s is a byte (wraps at 256); a 60 s soak from a fresh boot
            # never wraps, so any decrease means a reset.
            assert h["uptime_s"] >= up_prev, \
                f"0x704 uptime dropped {up_prev}->{h['uptime_s']} -> a reset occurred"
            up_prev = h["uptime_s"]
        assert samples >= int(SOAK_S * 0.8), f"only {samples} soak samples in {SOAK_S}s"


# -- J-002 flash-endurance helpers (mirror Block A) -------------------
def _broker():
    from broker.server import BrokerClient
    return BrokerClient(os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))


def _set_can_sp(channel, sp, bitrate=500_000):
    try:
        subprocess.run(["sudo", "ip", "link", "set", channel, "down"], check=False, timeout=5)
        subprocess.run(["sudo", "ip", "link", "set", channel, "type", "can", "bitrate",
                        str(bitrate), "sample-point", f"{sp:.3f}", "restart-ms", "200"],
                       check=True, timeout=5)
        subprocess.run(["sudo", "ip", "link", "set", channel, "txqueuelen", "1000"],
                       check=False, timeout=5)
        subprocess.run(["sudo", "ip", "link", "set", channel, "up"], check=True, timeout=5)
        time.sleep(0.4)
    except Exception as e:
        pytest.skip(f"could not set SP {sp} on {channel}: {e}")


def _power_cycle_to_app(vcu_profile, mlc_powered, timeout_s=8.0):
    from tools.firmware_test.can_observer import CanObserver
    c = _broker()
    rb = mlc_powered["relay_bit"]
    try:
        c.call("tca.write_pin", addr=0x20, port=0, pin=rb, value=False)
        time.sleep(2.0)
        c.call("tca.write_pin", addr=0x20, port=0, pin=rb, value=True)
    finally:
        c.close()
    with CanObserver(channel=vcu_profile["bus_acu"]) as obs:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if obs.last(M.ID_HEARTBEAT, extended=False) is not None:
                return True
            time.sleep(0.05)
    return False


def _reboot_to_bl(vcu_profile):
    from tools.firmware_test.acu_stim import AcuStim
    stim = AcuStim(channel=vcu_profile["bus_acu"])
    stim.start()
    try:
        for _ in range(6):
            stim.send_raw(0x002, bytes.fromhex("B007AD12"), is_extended_id=False)
            time.sleep(0.1)
    finally:
        stim.stop()
    time.sleep(2.5)


class TestJ002ReflashEndurance:
    def test_j002_reflash_cycles(self, mlc_powered, flasher, vcu_profile):
        """J-002: re-flash the running stub image over the BL N times; every cycle
        boots clean and the BL stays reachable. One bus-wedge re-up retry per flash."""
        from pathlib import Path
        from tools.firmware_test.can_observer import CanObserver
        from tools.firmware_test.flash_helper import CanFlasherError
        if not Path(STUB_BIN).is_file():
            pytest.skip(f"stub bin {STUB_BIN} not on the Pi (set ECU_HIL_STUB_BIN)")
        bus = vcu_profile["bus_flash"]
        app_sp = float(vcu_profile["bus_acu_sample_point"])
        addr = int(vcu_profile["app_flash_address"])
        to = float(vcu_profile["bl_flash_timeout_s"])
        n = int(os.environ.get("VCU_REFLASH_N", "2"))
        for i in range(n):
            assert _power_cycle_to_app(vcu_profile, mlc_powered), \
                f"cycle {i}: app didn't boot before flashing"
            _reboot_to_bl(vcu_profile)
            _set_can_sp(bus, _BL_SP)
            try:
                nodes = []
                for _ in range(5):
                    nodes = flasher.discover()
                    if nodes:
                        break
                    time.sleep(0.8)
                assert nodes, f"cycle {i}: BL not reachable"
                try:
                    flasher.flash(STUB_BIN, address=addr, verify=True, jump=True,
                                  extra_args=["--yes"], timeout_s=to)
                except CanFlasherError:
                    _set_can_sp(bus, _BL_SP)      # re-up the bus after a wedge, retry once
                    flasher.flash(STUB_BIN, address=addr, verify=True, jump=True,
                                  extra_args=["--yes"], timeout_s=to)
            finally:
                _set_can_sp(bus, app_sp)
            with CanObserver(channel=bus) as obs:
                deadline = time.monotonic() + 8.0
                booted = False
                while time.monotonic() < deadline:
                    if obs.last(M.ID_HEARTBEAT, extended=False) is not None:
                        booted = True
                        break
                    time.sleep(0.05)
            assert booted, f"cycle {i}: app didn't stream 0x100 after flash"


class TestJ003ContinuousDiffEnduranceSoak:
    def test_j003_continuous_diff_and_fault_soak(self, fresh_boot, inv_heartbeat, acu_inject,
                                                 pedals, start_button, pit_diag,
                                                 observe_acu, observe_inv, vcu_profile):
        """J-003 (FS Endurance): Continuously cycles slalom cornering, trail-braking,
        IMU dropout/recovery, and wheelspin Virtual LSD for VCU_ENDURANCE_S (default 180s)
        while verifying 0x100 heartbeat, ACTIVE FSM stability, and zero resets."""
        import math
        import threading
        from tools.firmware_test.acu_stim import AcuStim

        dur_s = float(os.environ.get("VCU_ENDURANCE_S", "180"))
        inv_heartbeat["vdc_ready"](500)
        acu_inject["set_precharge"](1)
        acu_inject["set_ams_state"](3)
        acu_inject["set_v_cell"](3600)
        if _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 4.0) is None:
            pytest.skip("FSM never reached WAIT_START_BRAKE -- DAC/gates")
        pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
        start_button["press"]()
        inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
        if _wait_fsm(pit_diag, lambda s: s == S.ACTIVE,
                     float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- DAC/gates")
        pedals["set_brake"](0)

        h0 = _health(observe_acu)
        if h0 is None:
            pytest.skip("no 0x704 health frame")
        rc0 = h0["reset_cause"]

        stim_acu = AcuStim(channel=vcu_profile["bus_acu"])
        stim_inv = AcuStim(channel=vcu_profile["bus_inv"])
        stim_acu.start()
        stim_inv.start()
        st = {"run": True, "angle": 0.0, "yaw": 0.0, "ay": 0.0, "send_imu": True,
              "erpm_l": 12000, "erpm_r": 12000}

        def stim_loop():
            while st["run"]:
                try:
                    stim_acu.send_raw(
                        M.ID_LWS_STANDARD,
                        M.encode_lws_standard(st["angle"], 0.0, ok=True, cal=True, trim=True),
                        is_extended_id=False,
                    )
                    if st["send_imu"]:
                        stim_acu.send_raw(
                            M.ID_XSENS_IMU,
                            M.encode_xsens_imu(st["yaw"], st["ay"], 0.0),
                            is_extended_id=False,
                        )
                    stim_inv.send_raw(
                        M.ID_DTI_ERPM_LEFT,
                        M.encode_dti_erpm(st["erpm_l"], input_voltage_v=500.0),
                        is_extended_id=False,
                    )
                    stim_inv.send_raw(
                        M.ID_DTI_ERPM_RIGHT,
                        M.encode_dti_erpm(st["erpm_r"], input_voltage_v=500.0),
                        is_extended_id=False,
                    )
                except Exception:
                    pass
                time.sleep(0.015)

        th = threading.Thread(target=stim_loop, daemon=True)
        th.start()
        try:
            deadline = time.monotonic() + dur_s
            step = 0
            while time.monotonic() < deadline:
                phase = step % 6
                if phase in (0, 1):
                    # Left / Right slalom cornering + varying APPS
                    ang = 25.0 if phase == 0 else -25.0
                    st.update(angle=ang, yaw=0.22 * (1 if phase == 0 else -1),
                              ay=3.3 * (1 if phase == 0 else -1), send_imu=True,
                              erpm_l=12000, erpm_r=12000)
                    pedals["set_brake"](0)
                    _apps(pedals, vcu_profile, 55)
                elif phase == 2:
                    # Trail-braking in corner
                    pedals["set_brake"](int(vcu_profile["brake_apps_raw"]) + 300)
                    _apps(pedals, vcu_profile, 45)
                elif phase == 3:
                    # IMU dropout -> MingoMode fallback
                    pedals["set_brake"](0)
                    st.update(send_imu=False)
                elif phase == 4:
                    # Wheelspin +40% right -> Virtual LSD trim
                    st.update(send_imu=True, erpm_l=12000, erpm_r=16800)
                else:
                    # Zero-pedal lift-off
                    _apps(pedals, vcu_profile, 0)
                    st.update(angle=0.0, yaw=0.0, ay=0.0, erpm_l=12000, erpm_r=12000)

                time.sleep(1.0)
                step += 1

                assert observe_acu.last(M.ID_HEARTBEAT, extended=False) is not None, \
                    f"0x100 heartbeat lost at step {step}"
                fsm = pit_diag["read_fsm"](0.4)
                assert fsm == S.ACTIVE, f"FSM left ACTIVE ({S.name_of(fsm)}) at step {step}"
                h = _health(observe_acu)
                assert h is not None and h["reset_cause"] == rc0, \
                    f"reset_cause changed during endurance soak at step {step}"
                fl = observe_inv.last(M.ID_DTI_SET_CURRENT_LEFT, extended=False)
                fr = observe_inv.last(M.ID_DTI_SET_CURRENT_RIGHT, extended=False)
                assert fl is not None and fr is not None, \
                    f"DTI current frames missing at step {step}"
        finally:
            st["run"] = False
            time.sleep(0.05)
            stim_acu.stop()
            stim_inv.stop()
