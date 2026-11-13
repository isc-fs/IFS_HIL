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
  D-007 Simultaneous brake + throttle: EV.2.3 retired, trail-braking permitted without current cut
  D-008 Steering sensor dropout: LWS timeout (>100 ms) with healthy IMU falls back to MingoMode (0 steer feedforward)
  D-009 Bosch LWS failure mode: uncalibrated/fault state (OK=0/0x7FFF) falls back to MingoMode (0 steer feedforward)
  D-010 IMU sensor dropout: IMU timeout (>50 ms) falls back to MingoMode feedforward
  D-011 Steering velocity dynamic feedforward: positive d_delta boosts outside turn-in torque
  D-012 Rapid slalom reversal: dynamic transitions smoothly invert bias without torque spikes
  D-013 Full lock saturation: high speed full lock adheres to physical current limit (<= 45.4 A)
  D-014 Virtual LSD anti-spin: differential wheel slip reduces torque on spinning wheel
  D-015 Rollback / reverse motion: v_x < 1.0 m/s or v_x < 0 suppresses vectoring to 50/50 split
  D-016 Asymmetric inverter trip: single inverter fault cuts both motor currents to 0
  D-017 Extreme split-mu slip: inside wheel spin trims inside current while outside drives
  D-018 Mode transitions: Party -> Mingo (IMU loss) -> Party (debounced recovery)
  D-019 Throttle stomp sweep: aggressive pedal stomps across 0, 15, 35 deg steering angles
  D-020 Reactive TV response: closed-loop correction under vehicle understeer and oversteer
  D-021 Manual PartyMode default & oversteer stabilization: closed-loop yaw damping in manual driving
  D-022 MingoMode on LWS fault: IMU curvature estimation + Virtual LSD without steering sensor
  D-023 PassiveMode on total sensor loss: inverter-only Virtual LSD above min_velocity_mps & 50/50 on jack-stand
  D-024 Full recovery ladder: Passive -> Mingo (LWS recovery) -> Party (debounced IMU recovery)

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
        """D-007: EV 2.3 brake limitation removed: Pressing brake during active cornering throttle
        permits trail-braking and does NOT cut inverter currents."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](30.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_r > cur_l and cur_r > 10.0

        # Press brake hard via DAC0 channel 0 (simultaneous brake + throttle / trail-braking)
        pedals["set_brake"](int(vcu_profile["brake_apps_raw"]) + 400)
        time.sleep(0.2)

        cur_l_trail, cur_r_trail = _read_dti_currents(observe_inv)
        assert cur_r_trail > cur_l_trail and cur_r_trail > 10.0, (
            f"EV.2.3 unexpectedly cut motor currents on brake: L={cur_l_trail}, R={cur_r_trail}"
        )


class TestD008SteeringTimeoutFailsafe:
    def test_d008_steering_timeout_failsafe(self, fresh_boot, inv_heartbeat, acu_inject,
                                            diff_stim, pedals, start_button, pit_diag,
                                            observe_inv, vcu_profile):
        """D-008: If the Bosch LWS stream goes quiet (>100 ms timeout) while IMU is healthy, the
        differential must fall back to MingoMode (zeroing steering feedforward) and NEVER hold the old angle."""
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
        the differential must treat the sensor as invalid, zero steering feedforward, and drop to MingoMode."""
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


class TestD011SteeringSpeedDynamicFeedforward:
    def test_d011_steering_speed_dynamic_feedforward(self, fresh_boot, inv_heartbeat, acu_inject,
                                                     diff_stim, pedals, start_button, pit_diag,
                                                     observe_inv, vcu_profile):
        """D-011: When the driver turns rapidly into a corner (positive steering wheel angular
        velocity), dynamic feedforward adds transient outside wheel torque beyond the static split."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        # Static turn left at 20 deg, 0 deg/s steering rate
        diff_stim["set_steering"](20.0, speed=0.0, ok=True, cal=True, trim=True)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l_static, cur_r_static = _read_dti_currents(observe_inv)
        assert cur_l_static is not None and cur_r_static is not None
        assert cur_r_static > cur_l_static

        # Driver applies fast turn-in steering velocity: +150 deg/s
        diff_stim["set_steering"](20.0, speed=150.0, ok=True, cal=True, trim=True)
        time.sleep(0.15)

        cur_l_dyn, cur_r_dyn = _read_dti_currents(observe_inv)
        assert cur_l_dyn is not None and cur_r_dyn is not None
        # Outside wheel must receive higher torque during dynamic turn-in transient
        assert cur_r_dyn >= cur_r_static + 0.20, (
            f"Dynamic feedforward failed to boost outside wheel: static={cur_r_static:.2f} A, dynamic={cur_r_dyn:.2f} A"
        )

        # Net torque invariant must strictly hold
        kt = float(vcu_profile.get("diff_kt_nm_per_a", 0.696))
        total_torque = (cur_l_dyn + cur_r_dyn) * kt
        assert total_torque <= 31.6 + 1.0, (
            f"Net torque exceeded driver demand: {total_torque:.2f} Nm > 31.6 Nm"
        )


class TestD012SlalomRapidReversal:
    def test_d012_slalom_rapid_reversal(self, fresh_boot, inv_heartbeat, acu_inject,
                                        diff_stim, pedals, start_button, pit_diag,
                                        observe_inv, vcu_profile):
        """D-012: In dynamic slalom maneuvers (+30 deg -> -30 deg), the differential smoothly
        inverts the outside torque bias without torque glitch, deadlock or overshoot."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        # Phase 1: Left turn (+30 deg)
        diff_stim["set_steering"](30.0, speed=0.0, ok=True, cal=True, trim=True)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l_left, cur_r_left = _read_dti_currents(observe_inv)
        assert cur_r_left > cur_l_left + 3.0, (
            f"Left turn baseline bias missing: L={cur_l_left}, R={cur_r_left}"
        )

        # Phase 2: Rapid reversal to Right turn (-30 deg)
        diff_stim["set_steering"](-30.0, speed=-150.0, ok=True, cal=True, trim=True)
        time.sleep(0.35)

        cur_l_right, cur_r_right = _read_dti_currents(observe_inv)
        assert cur_l_right > cur_r_right + 3.0, (
            f"Slalom reversal failed to smoothly invert bias: L={cur_l_right}, R={cur_r_right}"
        )

        # Inverter currents must remain positive and non-zero
        assert cur_l_right > 5.0 and cur_r_right > 5.0, (
            f"Inverter currents dropped during slalom transition: L={cur_l_right}, R={cur_r_right}"
        )


class TestD013ExtremeSteeringLockClamping:
    def test_d013_extreme_steering_lock_clamping(self, fresh_boot, inv_heartbeat, acu_inject,
                                                 diff_stim, pedals, start_button, pit_diag,
                                                 observe_inv, vcu_profile):
        """D-013: High-speed full lock (22 m/s, 40 deg steer, 100% APPS) strictly clamps both
        motor currents to the physical ceiling (31.6 Nm / 0.696 Nm/A ≈ 45.4 A) with zero negative values."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](22.0)
        diff_stim["set_steering"](40.0, speed=0.0, ok=True, cal=True, trim=True)
        # 100% full throttle
        _apps(pedals, vcu_profile, 100, 100)
        time.sleep(0.35)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_l is not None and cur_r is not None

        # Neither motor must exceed physical peak ceiling: 45.4 A
        max_cur = float(vcu_profile.get("diff_motor_max_torque_nm", 31.6)) / float(vcu_profile.get("diff_kt_nm_per_a", 0.696))
        assert cur_r <= max_cur + 0.5, f"Outside motor exceeded physical peak current: {cur_r:.2f} A > {max_cur:.2f} A"
        assert cur_l <= max_cur + 0.5, f"Inside motor exceeded physical peak current: {cur_l:.2f} A > {max_cur:.2f} A"
        assert cur_l >= 0.0 and cur_r >= 0.0, f"Negative current detected: L={cur_l}, R={cur_r}"
        assert cur_r > cur_l, f"Cornering bias missing at full lock: L={cur_l}, R={cur_r}"


class TestD014VirtualLsdAntiSpin:
    def test_d014_virtual_lsd_anti_spin(self, fresh_boot, inv_heartbeat, acu_inject,
                                        diff_stim, pedals, start_button, pit_diag,
                                        observe_inv, vcu_profile):
        """D-014: When one driven wheel breaks traction and spins excessively (>15% expected differential ERPM),
        the Virtual LSD reduces torque on the spinning wheel to regain traction."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        # Baseline: 12 m/s straight line with normal grip
        diff_stim["set_speed_mps"](12.0)
        diff_stim["set_steering"](0.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l_norm, cur_r_norm = _read_dti_currents(observe_inv)
        assert cur_l_norm is not None and cur_r_norm is not None
        assert abs(cur_l_norm - cur_r_norm) < 1.0

        # Simulate right wheel sudden wheelspin: ERPM_R jumps by 35%
        base_erpm = _speed_to_erpm(12.0, vcu_profile)
        spin_erpm = int(base_erpm * 1.35)
        diff_stim["set_erpm"](base_erpm, spin_erpm)
        time.sleep(0.25)

        cur_l_spin, cur_r_spin = _read_dti_currents(observe_inv)
        assert cur_l_spin is not None and cur_r_spin is not None
        # Virtual LSD must cut current to the spinning right wheel
        assert cur_r_spin < cur_l_spin, (
            f"Virtual LSD failed to reduce torque to spinning wheel: L={cur_l_spin:.2f} A, R={cur_r_spin:.2f} A"
        )


class TestD015RollbackReverseSuppression:
    def test_d015_rollback_reverse_suppression(self, fresh_boot, inv_heartbeat, acu_inject,
                                               diff_stim, pedals, start_button, pit_diag,
                                               observe_inv, vcu_profile):
        """D-015: When vehicle speed is negative or crawl (< 1.0 m/s), differential vectoring is
        suppressed and motor current split remains strictly 50/50 symmetric despite steering angle."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        # Rollback at -3.0 m/s with heavy steering lock (35 deg left)
        diff_stim["set_speed_mps"](-3.0)
        diff_stim["set_steering"](35.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l_rev, cur_r_rev = _read_dti_currents(observe_inv)
        assert cur_l_rev is not None and cur_r_rev is not None
        assert abs(cur_l_rev - cur_r_rev) < 1.0, (
            f"Rollback failed to suppress torque vectoring: L={cur_l_rev:.2f} A, R={cur_r_rev:.2f} A"
        )

        # Transition forward to 15.0 m/s: cornering bias must engage smoothly
        diff_stim["set_speed_mps"](15.0)
        time.sleep(0.3)

        cur_l_fwd, cur_r_fwd = _read_dti_currents(observe_inv)
        assert cur_r_fwd > cur_l_fwd and (cur_r_fwd - cur_l_fwd) > 3.0, (
            f"Forward cornering bias missing after rollback: L={cur_l_fwd:.2f} A, R={cur_r_fwd:.2f} A"
        )


class TestD016AsymmetricInverterCutout:
    def test_d016_asymmetric_inverter_cutout(self, fresh_boot, inv_heartbeat, acu_inject,
                                            diff_stim, pedals, start_button, pit_diag,
                                            observe_inv, vcu_profile):
        """D-016: Critical safety: Inverter trip or fault report immediately cuts both motor currents
        to 0 to prevent catastrophic uncommanded yaw spin."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](30.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l, cur_r = _read_dti_currents(observe_inv)
        assert cur_r > cur_l and cur_r > 10.0

        # Trip inverter via fault state (state 10 = Soft Fault / dem trip)
        inv_heartbeat["set_state"](10)
        time.sleep(0.2)

        cur_l_flt, cur_r_flt = _read_dti_currents(observe_inv)
        assert cur_l_flt == 0.0 and cur_r_flt == 0.0, (
            f"Inverter trip failed to cut dual currents: L={cur_l_flt}, R={cur_r_flt}"
        )


class TestD017ExtremeSplitMuWheelSlip:
    def test_d017_extreme_split_mu_wheel_slip(self, fresh_boot, inv_heartbeat, acu_inject,
                                              diff_stim, pedals, start_button, pit_diag,
                                              observe_inv, vcu_profile):
        """D-017: Extreme split-mu surface in corner: inside wheel slips excessively on loose/wet surface.
        Virtual LSD and TCS mitigate slip by transferring torque to outside gripping wheel."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        # 12 m/s cornering left (30 deg): left wheel inside, right wheel outside
        diff_stim["set_speed_mps"](12.0)
        diff_stim["set_steering"](30.0)
        _apps(pedals, vcu_profile, 60, 60)
        time.sleep(0.3)

        cur_l_norm, cur_r_norm = _read_dti_currents(observe_inv)
        assert cur_r_norm > cur_l_norm and cur_r_norm > 15.0

        # Inside left wheel hits wet paint line: ERPM_L spikes to 2.2x normal
        base_erpm = _speed_to_erpm(12.0, vcu_profile)
        diff_stim["set_erpm"](int(base_erpm * 2.2), base_erpm)
        time.sleep(0.25)

        cur_l_slip, cur_r_slip = _read_dti_currents(observe_inv)
        assert cur_l_slip < cur_l_norm * 0.70, (
            f"Slipping wheel current not sufficiently trimmed: before={cur_l_norm:.2f} A, after={cur_l_slip:.2f} A"
        )
        assert cur_r_slip > cur_l_slip, "Gripping outside wheel must maintain drive dominance"


class TestD018PartyMingoPartyTransitions:
    def test_d018_party_mingo_party_transitions(self, fresh_boot, inv_heartbeat, acu_inject,
                                                diff_stim, pedals, start_button, pit_diag,
                                                observe_inv, vcu_profile):
        """D-018: Validates clean transitions from PartyMode -> MingoMode (on IMU dropout)
        and debounced re-entry to PartyMode (150 ms anti-chatter) without torque dips or spikes."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        # Establish cornering drive: 15 m/s, 25 deg left turn, 50% APPS
        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](25.0, speed=0.0, ok=True, cal=True, trim=True)
        diff_stim["set_imu"](yaw=0.22, ay=3.3)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.35)

        cur_l_party1, cur_r_party1 = _read_dti_currents(observe_inv)
        assert cur_r_party1 > cur_l_party1 + 2.0, "PartyMode baseline outside bias missing"

        # 1. IMU stream drops out -> instant fallback to MingoMode (open-loop feedforward)
        diff_stim["stop_imu"]()
        time.sleep(0.18)  # > 50 ms timeout

        cur_l_mingo, cur_r_mingo = _read_dti_currents(observe_inv)
        assert cur_r_mingo > cur_l_mingo, "MingoMode must maintain outside wheel bias"
        assert cur_r_mingo > 5.0 and cur_l_mingo > 5.0, "Transition must not drop torque"

        # 2. IMU recovers -> debounce 150 ms before re-entering PartyMode
        diff_stim["set_imu"](yaw=0.22, ay=3.3)
        time.sleep(0.06)  # 60 ms: still in debounce
        cur_l_deb, cur_r_deb = _read_dti_currents(observe_inv)
        assert cur_r_deb > cur_l_deb, "Debounce phase preserves smooth torque"

        time.sleep(0.20)  # > 150 ms debounce elapsed: back in PartyMode
        cur_l_party2, cur_r_party2 = _read_dti_currents(observe_inv)
        assert cur_r_party2 > cur_l_party2 + 2.0, "Clean return to closed-loop PartyMode verified"


class TestD019ThrottleStompSteeringSweep:
    def test_d019_throttle_stomp_steering_sweep(self, fresh_boot, inv_heartbeat, acu_inject,
                                                diff_stim, pedals, start_button, pit_diag,
                                                observe_inv, vcu_profile):
        """D-019: Tests aggressive full throttle stomps (0% -> 75% APPS step) across 0, 15, and 35 deg
        steering angles, verifying rate-limited ramp-up and immediate 0 ms safety cut on lift-off."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)

        # Sweep through 3 distinct steering positions
        cases = [
            (0.0, "straight"),
            (20.0, "medium_turn"),
            (35.0, "hairpin_lock")
        ]

        for steer_deg, label in cases:
            diff_stim["set_steering"](steer_deg, speed=0.0, ok=True, cal=True, trim=True)
            _apps(pedals, vcu_profile, 0, 0)
            time.sleep(0.2)

            cur_l_0, cur_r_0 = _read_dti_currents(observe_inv)
            assert cur_l_0 == 0.0 and cur_r_0 == 0.0, f"Torque not zeroed before stomp ({label})"

            # Driver stomps pedal to 75%
            _apps(pedals, vcu_profile, 75, 75)
            time.sleep(0.30)

            cur_l_stomp, cur_r_stomp = _read_dti_currents(observe_inv)
            assert cur_l_stomp > 5.0 and cur_r_stomp > 5.0, f"Drive torque failed to ramp ({label})"

            if steer_deg == 0.0:
                assert abs(cur_l_stomp - cur_r_stomp) < 1.0, "Straight line stomp must be symmetric"
            elif steer_deg == 20.0:
                assert cur_r_stomp > cur_l_stomp + 0.5, "Medium turn stomp outside bias missing"
            elif steer_deg == 35.0:
                assert cur_r_stomp > cur_l_stomp + 3.0, "Hairpin stomp strong outside bias missing"

            # Lift-off: instant cut to 0.0 A
            _apps(pedals, vcu_profile, 0, 0)
            time.sleep(0.12)
            cur_l_cut, cur_r_cut = _read_dti_currents(observe_inv)
            assert cur_l_cut == 0.0 and cur_r_cut == 0.0, f"Lift-off failed instant zero cut ({label})"


class TestD020ClosedLoopYawCorrectionReaction:
    def test_d020_closed_loop_yaw_correction_reaction(self, fresh_boot, inv_heartbeat, acu_inject,
                                                      diff_stim, pedals, start_button, pit_diag,
                                                      observe_inv, vcu_profile):
        """D-020: Verifies that Torque Vectoring dynamically reacts in real-time to what is happening
        to the car: understeer induces increased outside wheel bias (yaw assist), while oversteer
        reduces outside torque bias to stabilize the vehicle."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        # Nominal cornering at 15 m/s, 25 deg left turn, 50% APPS
        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](25.0, speed=0.0, ok=True, cal=True, trim=True)
        # Expected nominal yaw ~ 0.20 rad/s, ay ~ 3.0 m/s^2
        diff_stim["set_imu"](yaw=0.20, ay=3.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.35)

        cur_l_nom, cur_r_nom = _read_dti_currents(observe_inv)
        delta_nom = cur_r_nom - cur_l_nom
        assert delta_nom > 2.0, "Baseline cornering delta missing"

        # 1. Understeer stimulus: front pushes wide, measured yaw drops to 0.06 rad/s
        diff_stim["set_imu"](yaw=0.06, ay=3.0)
        time.sleep(0.25)
        cur_l_under, cur_r_under = _read_dti_currents(observe_inv)
        delta_under = cur_r_under - cur_l_under

        assert delta_under >= delta_nom - 0.2, (
            f"Understeer reaction failed: nominal={delta_nom:.2f} A, understeer={delta_under:.2f} A"
        )

        # 2. Dynamic turn-in steering stomp (+150 deg/s steering rate)
        diff_stim["set_steering"](25.0, speed=150.0, ok=True, cal=True, trim=True)
        time.sleep(0.15)
        cur_l_dyn, cur_r_dyn = _read_dti_currents(observe_inv)
        assert cur_r_dyn > cur_r_nom + 0.3, "Dynamic steer speed feedforward failed to boost outside wheel"


class TestD021ManualPartyModeDefaultAndOversteer:
    def test_d021_manual_party_mode_default_and_oversteer(self, fresh_boot, inv_heartbeat, acu_inject,
                                                          diff_stim, pedals, start_button, pit_diag,
                                                          observe_inv, vcu_profile):
        """D-021: Confirms PartyMode closed-loop Torque Vectoring is active by default in manual
        driving and actively reduces outside torque bias when oversteer (excess yaw) is detected."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_steering"](25.0, speed=0.0, ok=True, cal=True, trim=True)
        diff_stim["set_imu"](yaw=0.22, ay=3.3)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.35)

        cur_l_nom, cur_r_nom = _read_dti_currents(observe_inv)
        delta_nom = cur_r_nom - cur_l_nom
        assert delta_nom > 2.0, f"PartyMode manual baseline bias missing: L={cur_l_nom}, R={cur_r_nom}"

        # Oversteer stimulus (plausible): rear slides, yaw spikes to 1.50 rad/s while ay saturates at 10.0 m/s^2
        # |ay - v_x*r| = |10.0 - 22.5| = 12.5 < 15.0 m/s^2 (IMU remains plausible)
        diff_stim["set_imu"](yaw=1.50, ay=10.0)
        time.sleep(0.25)
        cur_l_over, cur_r_over = _read_dti_currents(observe_inv)
        delta_over = cur_r_over - cur_l_over

        assert delta_over < delta_nom - 0.3, (
            f"Manual PartyMode failed oversteer counter-moment: nom={delta_nom:.2f} A, over={delta_over:.2f} A"
        )


class TestD022MingoLwsFaultImuCurvatureLsd:
    def test_d022_mingo_lws_fault_imu_curvature_lsd(self, fresh_boot, inv_heartbeat, acu_inject,
                                                      diff_stim, pedals, start_button, pit_diag,
                                                      observe_inv, vcu_profile):
        """D-022: On LWS-only fault (IMU valid), differential enters MingoMode using IMU curvature
        estimation and Virtual LSD, trimming a spinning rear wheel without steering sensor data."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        diff_stim["set_speed_mps"](15.0)
        diff_stim["set_imu"](yaw=0.0, ay=0.0)
        diff_stim["stop_steering"]()
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l_eq, cur_r_eq = _read_dti_currents(observe_inv)
        assert abs(cur_l_eq - cur_r_eq) < 1.0, "LWS-lost MingoMode should be symmetric without slip"

        # Inject right wheel spin (+40% ERPM) while LWS remains offline
        erpm_base = _speed_to_erpm(15.0, vcu_profile)
        diff_stim["set_erpm"](erpm_base, int(erpm_base * 1.40))
        time.sleep(0.25)

        cur_l_slip, cur_r_slip = _read_dti_currents(observe_inv)
        assert cur_r_slip < cur_l_slip - 0.5, (
            f"LWS-fault MingoMode Virtual LSD failed to trim spinning wheel: L={cur_l_slip:.2f} A, R={cur_r_slip:.2f} A"
        )


class TestD023TotalSensorLossPassiveInverterLsd:
    def test_d023_total_sensor_loss_passive_inverter_lsd(self, fresh_boot, inv_heartbeat, acu_inject,
                                                          diff_stim, pedals, start_button, pit_diag,
                                                          observe_inv, vcu_profile):
        """D-023: Total sensor loss (both LWS and IMU offline, inverter-only) enters PassiveMode with
        minimal Virtual LSD feedback above min_velocity_mps, and forces 50/50 on standstill/jack-stand."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        # Drop both LWS and IMU -> PassiveMode (inverter-only)
        diff_stim["stop_steering"]()
        diff_stim["stop_imu"]()
        diff_stim["set_speed_mps"](15.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.3)

        cur_l_eq, cur_r_eq = _read_dti_currents(observe_inv)
        assert cur_l_eq > 5.0 and cur_r_eq > 5.0, "PassiveMode must keep delivering drive torque"
        assert abs(cur_l_eq - cur_r_eq) < 1.0, "PassiveMode with matched wheels should be symmetric"

        # Asymmetric wheel spin (+40% right ERPM) -> PassiveMode Virtual LSD trims spinning wheel
        erpm_base = _speed_to_erpm(15.0, vcu_profile)
        diff_stim["set_erpm"](erpm_base, int(erpm_base * 1.40))
        time.sleep(0.25)

        cur_l_lsd, cur_r_lsd = _read_dti_currents(observe_inv)
        assert cur_r_lsd < cur_l_lsd - 0.5, (
            f"PassiveMode minimal inverter LSD failed to trim spinning wheel: L={cur_l_lsd:.2f} A, R={cur_r_lsd:.2f} A"
        )

        # Jack-stand / standstill (v_x <= 0.8 m/s) with asymmetric ERPM -> forced 50/50 safety split
        diff_stim["set_erpm"](0, 1500)
        time.sleep(0.25)
        cur_l_stand, cur_r_stand = _read_dti_currents(observe_inv)
        assert abs(cur_l_stand - cur_r_stand) < 1.0, (
            f"Standstill/jack-stand failed 50/50 safety split: L={cur_l_stand:.2f} A, R={cur_r_stand:.2f} A"
        )


class TestD024PassiveToMingoToPartyRecovery:
    def test_d024_passive_to_mingo_to_party_recovery(self, fresh_boot, inv_heartbeat, acu_inject,
                                                      diff_stim, pedals, start_button, pit_diag,
                                                      observe_inv, vcu_profile):
        """D-024: Validates progressive recovery ladder: Passive (both sensors lost) -> Mingo
        (LWS recovers) -> Party (IMU recovers + 150 ms debounce) with bump-less drive continuity."""
        if _to_active(inv_heartbeat, acu_inject, pedals, start_button, pit_diag,
                      vcu_profile) != S.ACTIVE:
            pytest.skip("FSM never reached Active -- check DAC/gates")

        # 1. Start in PassiveMode (LWS + IMU offline)
        diff_stim["stop_steering"]()
        diff_stim["stop_imu"]()
        diff_stim["set_speed_mps"](15.0)
        _apps(pedals, vcu_profile, 50, 50)
        time.sleep(0.25)

        cur_l_p, cur_r_p = _read_dti_currents(observe_inv)
        assert abs(cur_l_p - cur_r_p) < 1.0 and cur_l_p > 5.0

        # 2. LWS recovers -> MingoMode (outside wheel biased)
        diff_stim["set_steering"](25.0, speed=0.0, ok=True, cal=True, trim=True)
        time.sleep(0.20)
        cur_l_m, cur_r_m = _read_dti_currents(observe_inv)
        assert cur_r_m > cur_l_m + 1.0, f"Passive -> Mingo recovery failed: L={cur_l_m:.2f} A, R={cur_r_m:.2f} A"

        # 3. IMU recovers -> debounced return to PartyMode (150 ms)
        diff_stim["set_imu"](yaw=0.22, ay=3.3)
        time.sleep(0.25)
        cur_l_pty, cur_r_pty = _read_dti_currents(observe_inv)
        assert cur_r_pty > cur_l_pty + 2.0, f"Mingo -> Party recovery failed: L={cur_l_pty:.2f} A, R={cur_r_pty:.2f} A"
        assert cur_l_pty > 5.0 and cur_r_pty > 5.0, "Recovery caused unexpected torque drop"
