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


# ---------------------------------------------------------------------------
# A-009 — firmware_info.reserved[0] == node ID  (#245 baseline check)
# ---------------------------------------------------------------------------

class TestA009FirmwareInfoNodeId:
    """Static check on the .bin we're about to flash: the node-ID
    embedded in the `bl_fwinfo_t` record must match the BL node ID we
    address the unit by. Same record D-043 verifies, hoisted into
    Block A so the boot-baseline scoreboard catches a misbuilt image
    before the bench wastes cycles flashing it.
    """

    def test_a009(self, ams_firmware_bin, ams_profile):
        from pathlib import Path
        data = Path(ams_firmware_bin).read_bytes()
        rec_offset = 0x400
        assert len(data) > rec_offset + 0x40, (
            f"bin too small ({len(data)} bytes); no firmware_info record."
        )
        rec = data[rec_offset:rec_offset + 0x40]
        reserved0 = int.from_bytes(rec[0x38:0x3C], "little")
        expected = int(ams_profile["bl_node_id"])
        assert reserved0 == expected, (
            f"firmware_info.reserved[0] = 0x{reserved0:08X}, "
            f"expected 0x{expected:08X}."
        )


# ---------------------------------------------------------------------------
# A-010 — 0x4A2[5] cockpit byte in Start = 0x80 sentinel
# ---------------------------------------------------------------------------

class TestA010CockpitByteSentinel:
    """`0x4A2[5]` packs the cockpit state. In Start, with no fixtures
    driving PF9/PF10, the byte should read exactly `0x80`:
      bit 7 = valid sentinel        (always set when firmware is running)
      bits 4..6 = reserved          (zero)
      bits 2..3 = mode_locked       (00 = Undecided, FSM hasn't latched yet)
      bit 1 = TSMS                  (low)
      bit 0 = DASH_CHG              (low)
    Any deviation means either the firmware lost the sentinel bit, the
    inputs are mis-read (FSM thinks TSMS / DASH are HIGH), or the mode
    locked early (would be 0x84 / 0x88).
    """

    # Unskip per AMS PR #251 (closes #246): cockpit-byte encoding
    # hoisted out of AMS_BMS_HIL_STUB and is now always-on. Verified on
    # the post-#251 dev tip (78eaa330) — 0x4A2[5] reads 0x80 in Start
    # with no PF9/PF10 fixtures driving the cockpit inputs.
    def test_a010(self, fresh_boot, observe_acu, ams_profile):
        # Wait one telemetry cycle past fresh_boot so we see the
        # post-boot steady-state cockpit byte, not a transient.
        time.sleep(int(ams_profile["tx_telemetry_period_ms"]) / 1000.0 + 0.2)
        f = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert f is not None, "no 0x4A2 frame after settle"
        cockpit_byte = f.data[5]
        assert cockpit_byte == 0x80, (
            f"0x4A2[5] = 0x{cockpit_byte:02X}, expected 0x80 "
            f"(valid + both inputs LOW + mode Undecided). "
            f"bit 7 (sentinel)={'1' if cockpit_byte & 0x80 else '0'}, "
            f"mode={(cockpit_byte >> 2) & 0x03}, "
            f"TSMS={(cockpit_byte >> 1) & 1}, "
            f"DASH={cockpit_byte & 1}."
        )


# ---------------------------------------------------------------------------
# A-011 — ECU TX matrix DLCs match contract  (post-#238)
# ---------------------------------------------------------------------------

# (ID, expected_dlc, label)
ECU_TX_DLC_CONTRACT = [
    (0x020, 1, "ok_precharge"),
    (0x12C, 2, "v_cell_min"),
    (0x131, 6, "vmin_module_a"),
    (0x132, 4, "vmin_module_b"),
    (0x133, 6, "vmax_module_a"),
    (0x134, 4, "vmax_module_b"),
    (0x135, 4, "currents"),
    (0x136, 6, "tmax_module_a"),
    (0x137, 6, "tmax_module_b"),
]


class TestA011EcuTxMatrixDlcs:
    """Every ECU-side TX frame must ship with its contract DLC.
    Pre-#238 the entire matrix shipped with DLC=0 (the `dlc << 16`
    misencoding), which made the receiving ECU silently ignore the
    bus. This test is the regression net: capture each frame from
    `observe_acu` and assert the DLC is what the contract says.
    """

    def test_a011(self, fresh_boot, observe_acu, ams_profile):
        # Wait long enough for at least one full slow-TX cycle
        # (EcuSlowTxMs = 250 ms) plus jitter slack.
        time.sleep(0.6)
        missing: list[str] = []
        wrong_dlc: list[str] = []
        for can_id, want_dlc, label in ECU_TX_DLC_CONTRACT:
            f = observe_acu.last(can_id, extended=False)
            if f is None:
                missing.append(f"0x{can_id:03X} ({label})")
                continue
            if f.dlc != want_dlc:
                wrong_dlc.append(
                    f"0x{can_id:03X} ({label}): got DLC={f.dlc}, "
                    f"expected {want_dlc}")
        assert not missing, (
            "ECU TX matrix frames not seen in the 600 ms window: "
            + ", ".join(missing))
        assert not wrong_dlc, (
            "ECU TX matrix DLC mismatch(es) — same shape as #238 pre-fix:\n  "
            + "\n  ".join(wrong_dlc))


# ---------------------------------------------------------------------------
# A-012 — FDCAN1 drops extended-ID frames at the HW filter  (post-#236)
# ---------------------------------------------------------------------------

class TestA012FdCan1RejectsExtendedIds:
    """The AMS configures FDCAN1's global filter to REJECT extended frames at
    the hardware gate (PR #236). Proven *directly* on the RX path: a STANDARD
    0x100 updates `dc_bus_V` (0x4A2[3..4]); an EXTENDED-format 0x100 must NOT
    -- the filter drops it before the firmware ever sees it. If this regresses,
    an extended 0x100 would update dc_bus and keep the VCU-stale predicate
    fresh, masking real bus loss.

    Bitrate-independent: a direct RX-filter observation with no FSM/VcuStale
    timing dependency. (The earlier method drove to Precharge and waited for a
    VcuStale->Error trip, but with the heartbeat paused the FSM holds Precharge
    to its 5 s timeout rather than tripping Error inside the 200 ms VcuStale
    window -- so the indirect method was fragile and failed at 1 Mbps.)
    """

    def test_a012(self, fresh_boot, observe_acu, ams_profile):
        import time
        from tools.firmware_test.acu_stim import AcuStim

        std_val = 100   # 0x0064
        ext_val = 222   # 0x00DE -- clearly distinct from std_val and from 0
        stim = AcuStim(channel=ams_profile["bus_acu"])
        stim.start()
        try:
            # Positive control: a STANDARD 0x100 DOES update dc_bus (0x4A2[3:5]),
            # so RX of the VCU heartbeat works and the filter test is meaningful.
            observe_acu.clear()
            got_std = False
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                stim.send_raw(M.ID_DC_BUS_VOLTAGE,
                              std_val.to_bytes(2, "little"), is_extended_id=False)
                f = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
                if f is not None and \
                        int.from_bytes(bytes(f.data)[3:5], "little") == std_val:
                    got_std = True
                    break
                time.sleep(0.05)
            assert got_std, (
                f"a STANDARD 0x100={std_val} never updated dc_bus (0x4A2[3:5]) "
                "-- RX of the VCU heartbeat is broken, can't test the filter.")

            # The check: an EXTENDED-ID 0x100 carrying a different value must
            # NOT update dc_bus -- the FDCAN1 global filter rejects extended
            # frames at the HW gate, so the firmware never sees this value.
            observe_acu.clear()
            seen = set()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                stim.send_raw(M.ID_DC_BUS_VOLTAGE,
                              ext_val.to_bytes(2, "little"), is_extended_id=True)
                f = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
                if f is not None:
                    seen.add(int.from_bytes(bytes(f.data)[3:5], "little"))
                time.sleep(0.05)
            assert ext_val not in seen, (
                f"dc_bus took {ext_val} from an EXTENDED-ID 0x100 -- the FDCAN1 "
                "filter is NOT rejecting extended frames (#236 regressed). "
                f"dc_bus values seen while only the extended frame flowed: "
                f"{sorted(seen)}.")
        finally:
            stim.stop()


# ---------------------------------------------------------------------------
# A-013 — firmware_info via pit-diag 0x6C6  (NEW per AMS #272)
# ---------------------------------------------------------------------------

class TestA013FirmwareInfoMatchesSource:
    """Per AMS #317 A-013: pit-diag `0x6C6` ships the firmware identity
    record (semver bytes 0..2, git hash bytes 3..6, BL node ID byte 7).
    Asserts the semver against the source `VERSION` file (the release-gate
    check) + the BL node ID, and that the git-hash field is populated.

    Full git-hash equality is NOT enforced on the bench: the Pi is a
    non-git rsync working copy, and the Docker firmware build embeds a
    fixed fallback hash because `firmware/` carries no `.git` (flagged to
    the AMS team). If a real git checkout is available at AMS_SOURCE_DIR,
    the hash is cross-checked and logged but not asserted.
    """

    def test_a013(self, fresh_boot, pit_diag, ams_profile):
        import os
        import subprocess
        import logging
        from pathlib import Path

        # Expected semver from the firmware source's VERSION file. Default
        # to the rsync'd AMS source under the repo's firmware/ dir; override
        # with AMS_SOURCE_DIR.
        default_src = Path(__file__).resolve().parents[3] / "firmware"
        src = Path(os.environ.get("AMS_SOURCE_DIR", str(default_src)))
        version_file = src / "VERSION"
        if not version_file.exists():
            pytest.skip(
                f"VERSION not found at {version_file}. Set AMS_SOURCE_DIR to "
                "the firmware source the .bin was built from.")
        major, minor, patch = (
            int(x) for x in version_file.read_text().strip().split("."))

        # Wait for pit-diag scan (0x6C6 is near the end of the burst).
        pit_diag.wait_for_scan()
        fw_id = pit_diag.wait_for(M.ID_PIT_DIAG_FW_ID)
        assert len(fw_id) == 8, f"0x6C6 dlc != 8 (got {len(fw_id)})"

        # Semver -- the release-gate assertion.
        assert fw_id[0] == major, f"0x6C6[0] semver major = {fw_id[0]}, expected {major}"
        assert fw_id[1] == minor, f"0x6C6[1] semver minor = {fw_id[1]}, expected {minor}"
        assert fw_id[2] == patch, f"0x6C6[2] semver patch = {fw_id[2]}, expected {patch}"

        # BL node ID.
        expected_node_id = int(ams_profile["bl_node_id"])
        assert fw_id[7] == expected_node_id, (
            f"0x6C6[7] bl_node_id = 0x{fw_id[7]:02X}, "
            f"expected 0x{expected_node_id:02X}")

        # Git-hash field must at least be populated (non-zero).
        git_bytes = bytes(fw_id[3:7])
        assert any(git_bytes), (
            "0x6C6[3..6] git_hash is all-zero -- firmware_info hash field "
            "not populated.")
        # Cross-check against a real checkout if one is available; log-only,
        # since the HIL Docker build embeds a fallback hash (firmware/ has
        # no .git) and the Pi has no git at all.
        try:
            git_hash = subprocess.check_output(
                ["git", "rev-parse", "--short=8", "HEAD"], cwd=src,
                timeout=2, text=True, stderr=subprocess.DEVNULL).strip()
            if git_bytes != bytes.fromhex(git_hash)[:4]:
                logging.getLogger(__name__).warning(
                    "0x6C6 git_hash %s != source HEAD %s -- expected on the "
                    "HIL Docker build (firmware/ has no .git -> fallback).",
                    git_bytes.hex(), git_hash)
        except Exception:
            pass   # no git checkout at src (the Pi is non-git) -- semver is enough
