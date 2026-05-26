"""
Block A — Boot & bring-up.

Implements A-001..A-008 from `isc-fs/IFS08-CE-AMS#123` against the
chain-less rig (firmware built with `-DAMS_BMS_HIL_STUB=1`).

| Test  | What it checks                                          | Status        |
|-------|---------------------------------------------------------|---------------|
| A-001 | Relays open on power-up (PB5/6/7 LOW within 50 ms)      | needs probe   |
| A-002 | Bootloader discoverable on FDCAN2                       | implemented   |
| A-003 | App flashes via BL + jump                               | implemented   |
| A-004 | MainTask reaches Start within boot grace                | implemented   |
| A-005 | Stub seeder populates BmsState                          | implemented   |
| A-006 | 0x4A1 pack frame decodes correctly                      | implemented   |
| A-007 | 0x4A2 temps + heartbeat decodes correctly               | implemented   |
| A-008 | Telemetry cadence 500 ms ± 20 ms over 60 s              | implemented   |
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# A-001 — Relays open on power-up
# ---------------------------------------------------------------------------

class TestA001RelaysOpenOnPowerUp:
    """Per the test plan, PB5 (AIR+), PB6 (AIR-), PB7 (Precharge) must
    read LOW within 50 ms of power-on. The bench rig has no logic-
    analyser probes on the SLOT1_DIG* lines, so we can't time the
    transition directly. As a proxy, this test would observe via a
    TCA9555 input wired to the slot's relay-control pins — which the
    BACKPLANE_HIL doesn't currently route."""

    # PB5 (AIR+), PB6 (AIR-), PB7 (Precharge) are routed through the
    # MLC2 carrier header to MCP3208 U11 ("ADC3", broker idx=2):
    #   PB5 -> ADC3 ch1
    #   PB6 -> ADC3 ch2
    #   PB7 -> ADC3 ch3
    # (PB4 -> ADC3 ch0 is a separate signal, not part of the AIR/
    # precharge set.) STM32H7 cold reset leaves GPIOs as input-Z;
    # MX_GPIO_Init in app boot then sets PB5/6/7 as outputs LOW.
    # We assert all three read below LOW_THRESHOLD_V within 50 ms of
    # the TCA9555 relay-energise write (= t=0 anchor).
    def test_a001_relays_open_within_50ms(self, mlc_powered):
        from broker.server import BrokerClient
        ADC_IDX        = 2          # MCP3208 U11 ("ADC3")
        AIR_P_CH       = 1          # PB5
        AIR_N_CH       = 2          # PB6
        PRECHARGE_CH   = 3          # PB7
        LOW_THRESHOLD_V = 0.5       # CMOS-LOW guard; bench floor ~10 mV

        client = BrokerClient(os.environ.get(
            "HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))
        try:
            relay_bit = mlc_powered["relay_bit"]
            # Drop, settle, then assert t=0 anchor exactly before energise.
            client.call("tca.write_pin", addr=0x20, port=0,
                        pin=relay_bit, value=False)
            time.sleep(1.5)
            t0 = time.monotonic()
            client.call("tca.write_pin", addr=0x20, port=0,
                        pin=relay_bit, value=True)

            # Spec: each signal must READ LOW "within 50 ms of power-on"
            # = each signal must have CONVERGED to LOW by the 50 ms mark.
            # The 0..50 ms window is the chip's cold-reset + BL + app-
            # init transient -- during it GPIOs are input-Z and the ADC
            # will read whatever bias is on the trace (typically mid-
            # rail). What matters is the steady-state LOW after the
            # window, not the transient.
            #
            # Capture the full 0..100 ms trajectory so a failing trace
            # surfaces the convergence time; assert all three are LOW
            # at t >= 50 ms.
            channels = [(AIR_P_CH, "AIR+"),
                        (AIR_N_CH, "AIR-"),
                        (PRECHARGE_CH, "Precharge")]
            trace: list[tuple[float, dict[int, float]]] = []
            deadline = t0 + 0.100
            while time.monotonic() < deadline:
                t_ms = (time.monotonic() - t0) * 1e3
                sample = {ch: client.call("adc.read_voltage",
                                          idx=ADC_IDX, channel=ch)
                          for ch, _ in channels}
                trace.append((t_ms, sample))

            # Find the steady-state sample window: first sample at-or-
            # after t = 50 ms, and every sample after it must be LOW.
            post_50ms = [(t, s) for t, s in trace if t >= 50.0]
            assert post_50ms, (
                "no ADC samples collected after 50 ms — broker too slow "
                f"(captured {len(trace)} samples in 100 ms)"
            )
            failures = [(t, s) for t, s in post_50ms
                        if any(s[ch] >= LOW_THRESHOLD_V for ch, _ in channels)]
            if failures:
                trace_lines = "\n  ".join(
                    f"t={t:6.1f} ms  AIR+={s[AIR_P_CH]:.3f}  "
                    f"AIR-={s[AIR_N_CH]:.3f}  Pre={s[PRECHARGE_CH]:.3f}"
                    for t, s in trace)
                raise AssertionError(
                    f"relay-output GPIO(s) read HIGH after 50 ms boot "
                    f"window (threshold {LOW_THRESHOLD_V} V). "
                    f"{len(failures)} of {len(post_50ms)} post-50 ms "
                    f"samples failed. Full trace:\n  {trace_lines}"
                )
        finally:
            client.close()


# ---------------------------------------------------------------------------
# A-002 — Bootloader discoverable on FDCAN2
# ---------------------------------------------------------------------------

class TestA002BootloaderDiscoverable:

    def test_a002_bl_discover(self, mlc_powered, flasher, ams_profile):
        # Power-cycle so the BL is in its discover-listen window.
        from broker.server import BrokerClient
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        try:
            relay_bit = mlc_powered["relay_bit"]
            client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
            time.sleep(2.0)
            client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=True)
            time.sleep(float(ams_profile["mlc_boot_settle_s"]))
        finally:
            client.close()

        nodes = flasher.discover()
        assert nodes, (
            "No bootloaders replied on FDCAN2 within "
            f"{ams_profile['bl_discover_timeout_ms']} ms of power-on. "
            "Either the BL isn't flashed, or the auto-jump window is "
            "shorter than `mlc_boot_settle_s`."
        )
        assert len(nodes) == 1, (
            f"Expected exactly one node on the BL bus, got {len(nodes)} "
            "(distinct node IDs need provisioning if multiple carriers)."
        )
        n = nodes[0]
        assert n.node_id == flasher.node_id, (
            f"BL replied with node 0x{n.node_id:02X}, expected "
            f"0x{flasher.node_id:02X} (per ams_profile.bl_node_id)."
        )


# ---------------------------------------------------------------------------
# A-003 — App flashes via BL + jump
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def ams_firmware_bin(ams_profile) -> Path:
    """Path to the AMS app .bin built with `-DAMS_BMS_HIL_STUB=1`.
    Default `/tmp/AMS.bin`; override with `AMS_FIRMWARE_BIN`. Skips
    if missing so off-bench `pytest tests/` stays clean."""
    p = Path(os.environ.get("AMS_FIRMWARE_BIN", "/tmp/AMS.bin"))
    if not p.is_file():
        pytest.skip(f"AMS firmware not found at {p} (set AMS_FIRMWARE_BIN)")
    return p


class TestA003AppFlashesViaBL:

    def test_a003_flash_and_jump(self, mlc_powered, flasher, ams_firmware_bin,
                                 ams_profile):
        # Get into BL state (power-cycle into the discover window).
        from broker.server import BrokerClient
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        try:
            relay_bit = mlc_powered["relay_bit"]
            client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
            time.sleep(2.0)
            client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=True)
            time.sleep(float(ams_profile["mlc_boot_settle_s"]))
        finally:
            client.close()

        nodes = flasher.discover()
        assert nodes, "BL not reachable; cannot flash."

        r = flasher.flash(
            ams_firmware_bin,
            address=int(ams_profile["app_flash_address"]),
            verify=True,
            jump=True,
            timeout_s=float(ams_profile["bl_flash_timeout_s"]),
        )
        combined = (r.stdout or "") + "\n" + (r.stderr or "")
        assert "Done" in combined, (
            f"flash output missing 'Done' marker:\n"
            f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        )
        assert "jumped to app" in combined, (
            f"flash output missing jump confirmation:\n"
            f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        )


# ---------------------------------------------------------------------------
# A-004 — MainTask reaches Start within boot grace
# ---------------------------------------------------------------------------

class TestA004ReachesStart:

    def test_a004_first_telemetry_is_start(self, fresh_boot, ams_profile):
        first = fresh_boot["first_frame"]
        elapsed_ms = (fresh_boot["t_first_frame"] - fresh_boot["t_power_on"]) * 1000
        # First telemetry should land well before grace expires
        # (kSafetyBootGraceMs = 2000 ms). Allow generous slack for BL
        # auto-jump latency + first telemetry cycle.
        assert elapsed_ms < 5000, (
            f"First 0x4A0 took {elapsed_ms:.0f} ms from power-on; expected "
            "< 5000 ms. BL auto-jump may be delayed, or app boot is slow."
        )
        # State byte must read Start (=0); AMS_OK is allowed to be either
        # 0 (FSM has not transitioned out of Start yet) or 1 (already
        # left Start during this frame — racy but acceptable).
        assert first["state"] == M.FsmState.START, (
            f"First state byte was {first['state_name']} "
            f"(0x{first['state']:02X}); expected Start (0x00)."
        )


# ---------------------------------------------------------------------------
# A-005 — Stub seeder populates BmsState
# ---------------------------------------------------------------------------

class TestA005StubSeederPopulates:

    def test_a005_module_mask_and_cell_v(self, fresh_boot, observe_acu,
                                          ams_profile):
        # Wait one full telemetry cycle past first frame so the seeder
        # has had at least one BmsPollTask iteration (kBmsPollVoltMs = 250 ms).
        # First frame is already there from fresh_boot; sleep a beat
        # and grab the newest.
        time.sleep(int(ams_profile["tx_telemetry_period_ms"]) / 1000.0 + 0.2)

        frame = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
        assert frame is not None, "no 0x4A0 after settle"
        decoded = M.decode_telem_status(frame.data)

        assert decoded["module_online_mask"] == int(ams_profile["stub_module_online_mask"]), (
            f"module_online_mask = 0x{decoded['module_online_mask']:02X}; "
            f"expected 0x{int(ams_profile['stub_module_online_mask']):02X} "
            "(all 5 modules online under HIL_STUB seed)."
        )
        expected_cell_mV = int(ams_profile["stub_cell_mV"])
        assert decoded["min_cell_mV"] == expected_cell_mV, (
            f"min_cell_mV = {decoded['min_cell_mV']}; expected "
            f"{expected_cell_mV} (HIL_STUB seed)."
        )
        assert decoded["max_cell_mV"] == expected_cell_mV, (
            f"max_cell_mV = {decoded['max_cell_mV']}; expected "
            f"{expected_cell_mV}."
        )


# ---------------------------------------------------------------------------
# A-006 — 0x4A1 pack frame decodes correctly
# ---------------------------------------------------------------------------

class TestA006PackFrame:

    def test_a006_pack_voltage(self, fresh_boot, observe_acu, ams_profile):
        time.sleep(int(ams_profile["tx_telemetry_period_ms"]) / 1000.0 + 0.2)

        frame = observe_acu.last(M.ID_TELEM_PACK, extended=False)
        assert frame is not None, "no 0x4A1 after settle"
        decoded = M.decode_telem_pack(frame.data)

        expected = int(ams_profile["stub_expected_pack_mV"])
        assert decoded["pack_voltage_mV"] == expected, (
            f"pack_voltage_mV = {decoded['pack_voltage_mV']} mV; expected "
            f"{expected} mV "
            f"({ams_profile['stub_module_count']} × "
            f"{ams_profile['stub_cells_per_module']} × "
            f"{ams_profile['stub_cell_mV']} mV)."
        )
        # Current sensor is open-circuit on this rig; just verify the
        # field decodes into a reasonable int32 (not garbage / NaN).
        assert isinstance(decoded["filtered_mA"], int)


# ---------------------------------------------------------------------------
# A-007 — 0x4A2 temps + heartbeat decodes correctly
# ---------------------------------------------------------------------------

class TestA007TempsAndHeartbeat:

    def test_a007_temps(self, fresh_boot, observe_acu, ams_profile):
        time.sleep(int(ams_profile["tx_telemetry_period_ms"]) / 1000.0 + 0.2)

        frame = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert frame is not None, "no 0x4A2 after settle"
        decoded = M.decode_telem_temps(frame.data)

        expected_t = int(ams_profile["stub_temp_C"])
        assert decoded["min_tempC"] == expected_t, (
            f"min_tempC = {decoded['min_tempC']} °C; expected "
            f"{expected_t} °C (stub seed)."
        )
        assert decoded["max_tempC"] == expected_t
        assert decoded["avg_tempC"] == expected_t

    def test_a007_heartbeat_increments(self, fresh_boot, observe_acu,
                                       ams_profile, heartbeat_helper):
        # Wait for one telemetry cycle, snapshot, wait for the next.
        # Counter should advance by ≥ 1 modulo-256.
        period_ms = int(ams_profile["tx_telemetry_period_ms"])
        time.sleep(period_ms / 1000.0 + 0.1)
        baseline = heartbeat_helper["read"]()
        assert baseline is not None
        advanced = heartbeat_helper["wait_advance"](baseline, n=2)
        assert advanced is not None, (
            f"heartbeat counter didn't advance from {baseline} within "
            f"{2 * period_ms} ms. MainTask may be stalled."
        )


# ---------------------------------------------------------------------------
# A-008 — Telemetry cadence over 60 s
# ---------------------------------------------------------------------------

class TestA008TelemetryCadence:

    def test_a008_cadence_60s(self, fresh_boot, observe_acu, ams_profile):
        period_ms = int(ams_profile["tx_telemetry_period_ms"])
        jitter_ms = int(ams_profile["tx_telemetry_jitter_ms"])
        # Capture 60 s. Use 0x4A2 timestamps since the heartbeat byte
        # gives an unambiguous monotonic anchor and the cadence is
        # locked to the same MainTask loop as the other two IDs.
        observe_acu.clear()
        deadline = time.monotonic() + 60.0
        timestamps: list[float] = []
        last_hb: int | None = None
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
            if f is not None:
                hb = M.decode_telem_temps(f.data)["heartbeat"]
                if hb != last_hb:
                    timestamps.append(f.timestamp)
                    last_hb = hb
            time.sleep(period_ms / 1000.0 / 4)

        # Expect ~120 frames in 60 s at 500 ms; allow ±2 for boundary
        # windowing.
        expected = int(60_000 / period_ms)
        assert abs(len(timestamps) - expected) <= 2, (
            f"saw {len(timestamps)} 0x4A2 frames in 60 s, expected ~{expected}"
        )

        # Inter-frame periods within ±jitter_ms of the nominal period.
        deltas_ms = [(b - a) * 1000.0 for a, b in zip(timestamps, timestamps[1:])]
        outliers = [(i, d) for i, d in enumerate(deltas_ms)
                    if abs(d - period_ms) > jitter_ms]
        assert not outliers, (
            f"{len(outliers)} of {len(deltas_ms)} inter-frame periods "
            f"out of {period_ms} ± {jitter_ms} ms window. First few "
            f"offenders: {outliers[:3]}"
        )
