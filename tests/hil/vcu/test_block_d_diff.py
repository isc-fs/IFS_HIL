"""
Block D — Electronic Differential and Torque Vectoring (IFS09-CE-ECU).

Tests rear-axle torque splitting on HIL bench 1 across steering inputs and
accelerations using bench DACs and simulated CAN telemetry:
  D-001 Standstill baseline: 50/50 symmetric split (v_x < 0.8 m/s, APPS > 0)
  D-002 Straight-line high speed: 50/50 symmetric split in steering deadband
  D-003 Left cornering torque vectoring: outside (right) wheel biased (+delta_T)
  D-004 Right cornering torque vectoring: outside (left) wheel biased (+delta_T)
  D-005 Throttle pedal sweep via DACs: monotonic current scaling with cornering bias
  D-006 Pedal lift-off: zero throttle via DACs clears torque immediately
  D-007 EV.2.3 brake plausibility cut: brake via DAC zeroes dual inverter currents
  D-008 Steering sensor dropout: LWS timeout (>100 ms) falls back to passive 50/50
  D-009 Bosch LWS failure mode: uncalibrated/fault state (OK=0/0x7FFF) falls back to 50/50
  D-010 IMU sensor dropout: IMU timeout (>50 ms) falls back to MingoMode feedforward

Hardware interfacing:
  - DUT: IFS09-CE-ECU seated in MLC4 carrier slot.
  - Analog stimulus: bench DAC1 (idx 0) drives ECU ADC3:
      ch0 -> PF7 S_BRAKE (arming brake, EV.2.3 plausibility)
      ch1 -> PF8 APPS_1 (accelerator pedal 1)
      ch2 -> PF9 APPS_2 (accelerator pedal 2)
  - Inverter bus (kernel can0 / FDCAN1):
      TX commands observed: 0x021 (DTI Left Current), 0x022 (DTI Right Current),
                            0x181 (DTI Left Enable),  0x182 (DTI Right Enable).
      RX telemetry injected: 0x401 (Left ERPM), 0x402 (Right ERPM).
  - Vehicle / sensor bus (kernel can2 / FDCAN2):
      RX sensors injected: 0x2B0 (Bosch Motorsport LWS steering sensor, F02U.V02.894-01),
                           0x201 (Xsens MTi IMU).
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import pytest

from tools.firmware_test.vcu import can_map as M
from tools.firmware_test.vcu.can_map import VcuFsmState as S


def _skip_if_no_can(channel: str) -> None:
    if not Path(f"/sys/class/net/{channel}").exists():
        pytest.skip(f"SocketCAN interface {channel} not present")


def _wait_fsm(pit_diag, pred, timeout_s):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = pit_diag["read_fsm"](0.3)
        if last is not None and pred(last):
            return last
        time.sleep(0.05)
    return last


def _apps_raw(vcu_profile, which, pct):
    lo = int(vcu_profile[f"{which}_adc_min"])
    hi = int(vcu_profile[f"{which}_adc_max"])
    return int(lo + pct / 100.0 * (hi - lo))


def _apps(pedals, vcu_profile, p1, p2):
    pedals["set_apps1"](_apps_raw(vcu_profile, "apps1", p1))
    pedals["set_apps2"](_apps_raw(vcu_profile, "apps2", p2))


def _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag, vcu_profile):
    """Brings the ECU FSM from boot through Precharge and R2D to Active."""
    inv_heartbeat["vdc_ready"](500)
    acu_inject["set_precharge"](1)
    acu_inject["set_ams_state"](3)
    acu_inject["set_v_cell"](3600)
    if _wait_fsm(pit_diag, lambda s: s >= S.WAIT_START_BRAKE, 4.0) is None:
        pytest.skip("FSM never reached WAIT_START_BRAKE -- check DAC/gates")
    pedals["set_brake"](int(vcu_profile["brake_arm_raw"]) + 200)
    start_button["press"]()
    inv_heartbeat["set_state"](int(vcu_profile["inv_state_ready"]))
    s = _wait_fsm(pit_diag, lambda s: s == S.ACTIVE,
                  float(vcu_profile["r2d_delay_ms"]) / 1000.0 + 5.0)
    if s == S.ACTIVE:
        pedals["set_brake"](0)        # release the arming brake
        _apps(pedals, vcu_profile, 0, 0)
    return s


def _speed_to_erpm(v_mps: float, vcu_profile: dict) -> int:
    """Computes motor ERPM from longitudinal velocity (m/s)."""
    rw = float(vcu_profile.get("diff_wheel_radius_m", 0.203))
    g  = float(vcu_profile.get("diff_gear_ratio", 13.0))
    p  = 4  # pole pairs
    wheel_rads = v_mps / rw
    mech_rpm = (wheel_rads * 60.0) / (2.0 * math.pi) * g
    return int(round(mech_rpm * p))


@pytest.fixture
def diff_stim(vcu_profile):
    """Background injector for Electronic Differential sensors & actuators:
    - Bosch Motorsport LWS steering sensor (0x2B0 on can2/ACU)
    - Xsens MTi IMU (0x201 on can2/ACU)
    - DTI Inverter Telemetry ERPM (0x401 / 0x402 on can0/INV)
    """
    import threading
    from tools.firmware_test.acu_stim import AcuStim

    bus_acu = vcu_profile["bus_acu"]
    bus_inv = vcu_profile["bus_inv"]
    _skip_if_no_can(bus_acu)
    _skip_if_no_can(bus_inv)

    stim_acu = AcuStim(channel=bus_acu)
    stim_inv = AcuStim(channel=bus_inv)
    stim_acu.start()
    stim_inv.start()

    st = {
        "run": True,
        # Bosch LWS Standard (0x2B0)
        "send_lws": True,
        "steer_angle": 0.0,
        "steer_speed": 0.0,
        "lws_ok": True,
        "lws_cal": True,
        "lws_trim": True,
        # Xsens IMU (0x201)
        "send_imu": True,
        "yaw_rate": 0.0,
        "lat_accel": 0.0,
        "long_accel": 0.0,
        # DTI Telemetry (0x401 / 0x402)
        "send_dti": True,
        "erpm_l": 0,
        "erpm_r": 0,
        "vdc": 500.0,
    }

    def loop():
        while st["run"]:
            try:
                if st["send_lws"]:
                    payload_lws = M.encode_lws_standard(
                        st["steer_angle"], st["steer_speed"],
                        ok=st["lws_ok"], cal=st["lws_cal"], trim=st["lws_trim"]
                    )
                    stim_acu.send_raw(M.ID_LWS_STANDARD, payload_lws, is_extended_id=False)

                if st["send_imu"]:
                    payload_imu = M.encode_xsens_imu(
                        st["yaw_rate"], st["lat_accel"], st["long_accel"]
                    )
                    stim_acu.send_raw(M.ID_XSENS_IMU, payload_imu, is_extended_id=False)

                if st["send_dti"]:
                    payload_erpm_l = M.encode_dti_erpm(st["erpm_l"], input_voltage_v=st["vdc"])
                    payload_erpm_r = M.encode_dti_erpm(st["erpm_r"], input_voltage_v=st["vdc"])
                    stim_inv.send_raw(M.ID_DTI_ERPM_LEFT, payload_erpm_l, is_extended_id=False)
                    stim_inv.send_raw(M.ID_DTI_ERPM_RIGHT, payload_erpm_r, is_extended_id=False)
            except Exception:
                pass
            time.sleep(0.012)  # ~83 Hz cadence (well inside 50 ms / 100 ms timeouts)

    th = threading.Thread(target=loop, daemon=True)
    th.start()

    helpers = {
        "set_steering": lambda angle, speed=0.0, ok=True, cal=True, trim=True: st.update(
            steer_angle=float(angle), steer_speed=float(speed),
            lws_ok=bool(ok), lws_cal=bool(cal), lws_trim=bool(trim), send_lws=True
        ),
        "stop_steering": lambda: st.update(send_lws=False),
        "set_imu": lambda yaw=0.0, ay=0.0, ax=0.0: st.update(
            yaw_rate=float(yaw), lat_accel=float(ay), long_accel=float(ax), send_imu=True
        ),
        "stop_imu": lambda: st.update(send_imu=False),
        "set_speed_mps": lambda v: st.update(
            erpm_l=_speed_to_erpm(v, vcu_profile),
            erpm_r=_speed_to_erpm(v, vcu_profile),
            send_dti=True
        ),
        "set_erpm": lambda l, r: st.update(erpm_l=int(l), erpm_r=int(r), send_dti=True),
        "stop_all": lambda: st.update(send_lws=False, send_imu=False, send_dti=False),
    }

    yield helpers
    st["run"] = False
    time.sleep(0.05)
    stim_acu.stop()
    stim_inv.stop()


def _read_dti_currents(observe_inv, timeout_s=0.5):
    """Waits for and returns latest (current_left_apk, current_right_apk) from can0."""
    deadline = time.monotonic() + timeout_s
    cl, cr = None, None
    while time.monotonic() < deadline:
        fl = observe_inv.last(M.ID_DTI_SET_CURRENT_LEFT, extended=False)
        fr = observe_inv.last(M.ID_DTI_SET_CURRENT_RIGHT, extended=False)
        if fl is not None and fr is not None:
            cl = M.decode_dti_set_current(fl.data)["current_apk"]
            cr = M.decode_dti_set_current(fr.data)["current_apk"]
            return cl, cr
        time.sleep(0.02)
    return cl, cr


# =====================================================================
# Tests: Electronic Differential & Torque Vectoring
# =====================================================================

class TestD001StandstillSymmetricSplit:
    def test_d001_standstill_symmetric_split(self, fresh_boot, inv_heartbeat, acu_inject,
                                            diff_stim, pedals, start_button, pit_diag,
                                            observe_inv, vcu_profile):
        """D-001: At standstill (v_x < 0.8 m/s), torque is distributed 50/50 symmetrically
        between left and right motors regardless of steering angle."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        # Standstill: ERPM = 0, Steering = +15 deg
        diff_stim["set_speed_mps"](0.0)
        diff_stim["set_steering"](15.0)
        time.sleep(0.1)

        # Apply 50% accelerator pedal via DAC0
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_l is not None and cur_r is not None, "DTI set_current frames missing on can0"
        assert cur_l > 15.0 and cur_r > 15.0, f"Expected active current, got L={cur_l}, R={cur_r}"
        assert abs(cur_l - cur_r) < 1.0, f"Standstill torque not symmetric: L={cur_l} A, R={cur_r} A"

        # Drive Enable frames must both be asserted (Packet 0x0C)
        fen_l = observe_inv.last(M.ID_DTI_DRIVE_ENABLE_LEFT, extended=False)
        fen_r = observe_inv.last(M.ID_DTI_DRIVE_ENABLE_RIGHT, extended=False)
        assert fen_l is not None and M.decode_dti_drive_enable(fen_l.data)["drive_enabled"]
        assert fen_r is not None and M.decode_dti_drive_enable(fen_r.data)["drive_enabled"]


class TestD002StraightLineHighSpeed:
    def test_d002_straight_line_high_speed(self, fresh_boot, inv_heartbeat, acu_inject,
                                          diff_stim, pedals, start_button, pit_diag,
                                          observe_inv, vcu_profile):
        """D-002: At high velocity (v_x = 20 m/s ~ 72 km/h) with 0 deg steering angle (deadband),
        torque remains strictly symmetric 50/50."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](20.0)
        diff_stim["set_steering"](0.0)
        diff_stim["set_imu"](yaw=0.0, ay=0.0)
        time.sleep(0.1)

        _apps(pedals, vcu_profile, 60, 60)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_l is not None and cur_r is not None
        assert cur_l > 20.0 and cur_r > 20.0, f"Expected driving current, got L={cur_l}, R={cur_r}"
        assert abs(cur_l - cur_r) < 1.0, f"Straight-line bias detected: L={cur_l} A, R={cur_r} A"


class TestD003LeftTurnTorqueBias:
    def test_d003_left_turn_torque_bias(self, fresh_boot, inv_heartbeat, acu_inject,
                                        diff_stim, pedals, start_button, pit_diag,
                                        observe_inv, vcu_profile):
        """D-003: Cornering left (+30 deg steering) at 15 m/s:
        Inside wheel (Left) torque decreases; outside wheel (Right) torque increases.
        Net torque must remain <= total demanded torque (Strict APPS invariance)."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        # +30 deg steering input (Bosch Motorsport LWS frame 0x2B0, Little-Endian)
        diff_stim["set_steering"](30.0, speed=10.0, ok=True, cal=True, trim=True)
        time.sleep(0.2)

        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_l is not None and cur_r is not None

        # Outside wheel (Right) must receive strictly more current than Inside wheel (Left)
        delta_current = cur_r - cur_l
        assert delta_current > 3.0, (
            f"Left turn failed to bias outside wheel: L={cur_l:.2f} A, R={cur_r:.2f} A (delta={delta_current:.2f} A)"
        )

        # Net torque conservation check: T_left + T_right <= T_total (50% throttle = 31.6 Nm)
        kt = float(vcu_profile.get("diff_kt_nm_per_a", 0.696))
        total_torque = (cur_l + cur_r) * kt
        assert total_torque <= 31.6 + 1.0, (
            f"Net torque exceeded driver demand: {total_torque:.2f} Nm > 31.6 Nm"
        )


class TestD004RightTurnTorqueBias:
    def test_d004_right_turn_torque_bias(self, fresh_boot, inv_heartbeat, acu_inject,
                                         diff_stim, pedals, start_button, pit_diag,
                                         observe_inv, vcu_profile):
        """D-004: Cornering right (-30 deg steering) at 15 m/s:
        Inside wheel (Right) torque decreases; outside wheel (Left) torque increases.
        Net torque must remain <= total demanded torque."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        # -30 deg steering input (Bosch LWS negative angle)
        diff_stim["set_steering"](-30.0, speed=10.0, ok=True, cal=True, trim=True)
        time.sleep(0.2)

        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_l is not None and cur_r is not None

        # Outside wheel (Left) must receive strictly more current than Inside wheel (Right)
        delta_current = cur_l - cur_r
        assert delta_current > 3.0, (
            f"Right turn failed to bias outside wheel: L={cur_l:.2f} A, R={cur_r:.2f} A (delta={delta_current:.2f} A)"
        )

        kt = float(vcu_profile.get("diff_kt_nm_per_a", 0.696))
        total_torque = (cur_l + cur_r) * kt
        assert total_torque <= 31.6 + 1.0, (
            f"Net torque exceeded driver demand: {total_torque:.2f} Nm > 31.6 Nm"
        )


class TestD005AccelerationPedalSweep:
    def test_d005_acceleration_pedal_sweep(self, fresh_boot, inv_heartbeat, acu_inject,
                                           diff_stim, pedals, start_button, pit_diag,
                                           observe_inv, vcu_profile):
        """D-005: Sweep accelerator pedal via DACs across 20% -> 50% -> 80% throttle while cornering.
        Verifies that wheel currents scale monotonically with driver demand while maintaining vectoring bias."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](25.0)
        time.sleep(0.1)

        # 1. 20% throttle via DACs
        _apps(pedals, vcu_profile, 20, 20)
        time.sleep(0.3)
        cur_l_20, cur_r_20 = _read_dti_currents(observe_inv)

        # 2. 50% throttle via DACs
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)
        cur_l_50, cur_r_50 = _read_dti_currents(observe_inv)

        # 3. 80% throttle via DACs
        _apps(pedals, vcu_profile, 80, 80)
        time.sleep(0.3)
        cur_l_80, cur_r_80 = _read_dti_currents(observe_inv)

        assert cur_r_20 > cur_l_20, "20% throttle outside wheel bias missing"
        assert cur_r_50 > cur_l_50, "50% throttle outside wheel bias missing"
        assert cur_r_80 > cur_l_80, "80% throttle outside wheel bias missing"

        # Monotonic scaling
        assert cur_r_80 > cur_r_50 > cur_r_20, (
            f"Non-monotonic right motor scaling: {cur_r_20} -> {cur_r_50} -> {cur_r_80}"
        )
        assert cur_l_80 > cur_l_50 > cur_l_20, (
            f"Non-monotonic left motor scaling: {cur_l_20} -> {cur_l_50} -> {cur_l_80}"
        )


class TestD006ZeroThrottleLiftOff:
    def test_d006_zero_throttle_lift_off(self, fresh_boot, inv_heartbeat, acu_inject,
                                         diff_stim, pedals, start_button, pit_diag,
                                         observe_inv, vcu_profile):
        """D-006: Releasing accelerator pedal (0% throttle via DACs) zeroes both inverter currents
        immediately, even while cornering with high steering input."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](35.0)

        # Drive with 50% throttle first
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)
        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_r > cur_l and cur_r > 10.0

        # Lift off accelerator completely (0% via DACs)
        _apps(pedals, vcu_profile, 0, 0)
        time.sleep(0.2)

        cur_l_idle, cur_r_idle = _read_dti_currents(observe_inv)
        assert cur_l_idle == 0.0 and cur_r_idle == 0.0, (
            f"Expected 0 A on throttle lift-off, got L={cur_l_idle}, R={cur_r_idle}"
        )


class TestD007BrakeImplausibilityCut:
    def test_d007_ev_2_3_brake_implausibility_cut(self, fresh_boot, inv_heartbeat, acu_inject,
                                                  diff_stim, pedals, start_button, pit_diag,
                                                  observe_inv, vcu_profile):
        """D-007: Pressing brake (> BrakePressedRaw) during active cornering throttle immediately cuts
        both inverter currents to 0 per FS EV.2.3 rules."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](30.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_r > cur_l and cur_r > 10.0

        # Press brake hard via DAC0 channel 0
        pedals["set_brake"](int(vcu_profile["brake_apps_raw"]) + 400)
        time.sleep(0.2)

        cur_l_cut, cur_r_cut = _read_dti_currents(observe_inv)
        assert cur_l_cut == 0.0 and cur_r_cut == 0.0, (
            f"EV.2.3 failed to cut motor currents: L={cur_l_cut}, R={cur_r_cut}"
        )


class TestD008SteeringTimeoutFailsafe:
    def test_d008_steering_timeout_failsafe(self, fresh_boot, inv_heartbeat, acu_inject,
                                            diff_stim, pedals, start_button, pit_diag,
                                            observe_inv, vcu_profile):
        """D-008: If the Bosch LWS stream goes quiet (>100 ms timeout), the differential must
        immediately fall back to PASSIVE 50/50 split and NEVER hold the old steering angle."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](30.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert (cur_r - cur_l) > 3.0, "Active bias expected before timeout"

        # Silence Bosch LWS steering sensor stream
        diff_stim["stop_steering"]()
        time.sleep(0.25)  # > 100 ms steer_timeout_ms

        cur_l_to, cur_r_to = _read_dti_currents(observe_inv)
        assert cur_l_to is not None and cur_r_to is not None
        # Must return to symmetric 50/50 split
        assert abs(cur_l_to - cur_r_to) < 1.0, (
            f"Steering timeout failed to fall back to 50/50: L={cur_l_to} A, R={cur_r_to} A"
        )


class TestD009BoschLwsFailureModeFailsafe:
    def test_d009_bosch_lws_failure_mode_failsafe(self, fresh_boot, inv_heartbeat, acu_inject,
                                                  diff_stim, pedals, start_button, pit_diag,
                                                  observe_inv, vcu_profile):
        """D-009: When Bosch LWS reports failure mode (OK=0 or angle=0x7FFF per datasheet truth table),
        the differential must treat the sensor as invalid and fall back to symmetric 50/50 split."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](30.0, ok=True, cal=True, trim=True)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert (cur_r - cur_l) > 3.0

        # Inject failure mode: internal sensor fault (OK=0, CAL=0, TRIM=1, raw_angle=0x7FFF)
        diff_stim["set_steering"](0.0, ok=False, cal=False, trim=True)
        time.sleep(0.2)

        cur_l_flt, cur_r_flt = _read_dti_currents(observe_inv)
        assert cur_l_flt is not None and cur_r_flt is not None
        assert abs(cur_l_flt - cur_r_flt) < 1.0, (
            f"Sensor failure mode failed to fall back to 50/50: L={cur_l_flt} A, R={cur_r_flt} A"
        )


class TestD010ImuTimeoutFallback:
    def test_d010_imu_timeout_fallback(self, fresh_boot, inv_heartbeat, acu_inject,
                                        diff_stim, pedals, start_button, pit_diag,
                                        observe_inv, vcu_profile):
        """D-010: If the IMU stream goes quiet (>50 ms timeout) mid-corner, the differential must
        seamlessly fall back to MingoMode (kinematic feedforward) preserving steer assist without torque drop."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](30.0, ok=True, cal=True, trim=True)
        diff_stim["set_imu"](yaw=0.20, ay=3.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_r > cur_l and cur_r > 10.0

        # Silence IMU stream
        diff_stim["stop_imu"]()
        time.sleep(0.20)  # > 50 ms imu_timeout_ms

        cur_l_fb, cur_r_fb = _read_dti_currents(observe_inv)
        assert cur_l_fb is not None and cur_r_fb is not None
        # MingoMode still biases outside wheel on steering input
        assert cur_r_fb > cur_l_fb, (
            f"Fallback to MingoMode lost outside bias: L={cur_l_fb} A, R={cur_r_fb} A"
        )
        # Drive must NOT cut to 0
        assert cur_r_fb > 5.0 and cur_l_fb > 5.0, (
            f"IMU fallback erroneously cut drive torque: L={cur_l_fb} A, R={cur_r_fb} A"
        )
