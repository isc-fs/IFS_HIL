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
| A-009 | firmware_info.reserved[0] == 0x01                       | deferred (needs read-fwinfo RPC) |
| A-010 | 0x4A2[5] cockpit byte in Start = 0x80                   | implemented   |
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
    read LOW within 50 ms of power-on. The bench samples PB4..PB7 via
    ADC3 (MCP3208, broker idx 2) channels 0..3, so we just sweep all
    four relay GPIO readbacks for 50 ms post-power-on and assert
    everything stayed LOW the entire window."""

    def test_a001_relays_open_within_50ms(self, mlc_powered, relays_readback,
                                          ams_profile):
        if relays_readback is None:
            pytest.skip("relays_readback fixture disabled (missing *_adc_* "
                        "keys in ams_profile.yaml)")

        from broker.server import BrokerClient
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        relay_bit = mlc_powered["relay_bit"]
        try:
            client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=False)
            time.sleep(2.0)
            client.call("tca.write_pin", addr=0x20, port=0, pin=relay_bit, value=True)
            t_power_on = time.monotonic()
        finally:
            client.close()

        # Sample for 50 ms at ~5 ms cadence -- about 10 samples per pin.
        samples = relays_readback.sample_for(duration_s=0.050, period_s=0.005)
        assert samples, "no samples taken within 50 ms window"

        # Every sample must show all relay-control lines LOW.
        offenders = [(t, s) for (t, s) in samples
                     if s["air_p"] or s["air_n"] or s["prech"]]
        assert not offenders, (
            f"{len(offenders)} of {len(samples)} samples saw a relay-control "
            f"pin HIGH within 50 ms of power-on. First 3: "
            f"{[(round(t*1000, 1), s) for t, s in offenders[:3]]}. "
            f"Final voltages: {relays_readback.read_volts()}."
        )
        _ = t_power_on  # for future per-sample-vs-t_power_on diagnostics


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
# A-009 -- firmware_info record sanity (reserved[0] == 0x01)
# ---------------------------------------------------------------------------
# Per `isc-fs/IFS08-CE-AMS#193`: the firmware_info record at
# `0x08020400` carries `reserved[0] = 0x01` (build-time marker that the
# BL/app handshake agreed on the new node ID). The bench has no
# memory-read RPC yet (#193 "Test-infrastructure dependencies"); skip
# until either a `can-flasher read-fwinfo` subcommand or an equivalent
# broker RPC exists.

class TestA009FirmwareInfoReserved:

    @pytest.mark.skip(reason=(
        "A-009 needs a memory-read path (can-flasher `read-fwinfo` or "
        "an equivalent broker RPC) to fetch the `firmware_info` struct "
        "at 0x08020400 without SWD. Defer until that RPC lands."
    ))
    def test_a009_firmware_info_reserved(self):
        pass


# ---------------------------------------------------------------------------
# A-010 -- Cockpit byte sentinel in Start = 0x80
# ---------------------------------------------------------------------------
# Per `isc-fs/IFS08-CE-AMS#193`: at boot, before TSMS or DASH_CHG are
# driven, `0x4A2[5]` must be `0x80` -- sentinel bit set, mode = Undecided,
# both inputs low. This is the simplest end-to-end check that the
# encoder is on the HIL_STUB code path and the input GPIO reads are
# wired through correctly.

class TestA010CockpitByteSentinelInStart:

    def test_a010_cockpit_byte_in_start(self, fresh_boot, observe_acu,
                                        ams_profile):
        # First 0x4A2 lands at the same time as 0x4A0 in fresh_boot's
        # capture window. Settle one cycle so we're definitely looking at
        # a post-boot-grace frame (not a Start->Error in-flight one).
        time.sleep(int(ams_profile["tx_telemetry_period_ms"]) / 1000.0 + 0.2)
        frame = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
        assert frame is not None, "no 0x4A2 after settle"
        decoded = M.decode_telem_temps(frame.data)
        cb = decoded["cockpit"]
        assert cb["valid"], (
            f"cockpit byte sentinel (bit 7) missing: byte5=0x{cb['raw']:02X}. "
            f"Build may not be HIL_STUB, or firmware predates "
            f"isc-fs/IFS08-CE-AMS#190."
        )
        assert decoded["byte5"] == 0x80, (
            f"byte5 = 0x{decoded['byte5']:02X}; expected 0x80 "
            f"(sentinel, mode=Undecided, TSMS=0, DASH_CHG=0). Decoded: {cb}"
        )
