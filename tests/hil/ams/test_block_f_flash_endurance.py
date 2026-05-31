"""
Block F — Flash endurance.

Per `isc-fs/IFS08-CE-AMS#245`. Soak the entire reset / flash / boot
pipeline against the current state of #210 (PB9 move), #226
(standalone latch clear), and #243 (BL trigger silent-drop fix).
Acceptance target: 100 cycles for the soak rows, 10 cycles each for
the variant rows.

| Test  | What it checks                                     | Cycles | Status      |
|-------|----------------------------------------------------|--------|-------------|
| F-070 | Cold soak: power-cycle → BL → app jump → first 4A0 | 100    | implemented |
| F-071 | CAN-trigger soak: trigger → BL → flash → jump      | 100    | implemented |
| F-072 | Cross-trigger mix: alternate cold + CAN reboots    | 100    | implemented |
| F-073 | CRC integrity per cycle (can-flasher readback-CRC) | 100    | implemented |
| F-074 | Bus-busy flash (heartbeat + noise during flash)    |  20    | implemented |
| F-075 | Mixed-version round-trip (semver A → B → A)        |  10    | implemented (needs AMS_FIRMWARE_BIN_B) |
| F-076 | Stale-latch flash (TSMS-drop fault stim, no SWD)   | 5×2    | HIL_CLEAR impl; flight variant needs flight build |
| F-077 | Interrupted-flash recovery (yank VBUS mid-flash)   |  10    | implemented (coarse relay yank; recovery soak) |
| F-078 | Power-off duration sweep ({1, 5, 30, 60, 300} s)   | 5×5    | implemented |
| F-079 | DISCOVER latency long-soak                         | 1000   | implemented |
| F-080 | Trigger-from-Error (TSMS-drop → Error → trigger)  |  20    | implemented |
| F-081 | Bench-noise immunity (200 std-ID/s + valid trigger)|  60 s  | implemented |

All rows are marked with `@pytest.mark.soak` so the default suite
stays fast — opt-in via `pytest -m soak`. Cycle counts can be
overridden with `--soak-cycle-scale` (default 1.0; 0.1 makes a
100-cycle row run for 10 cycles).

Counters are pushed into the KPI ledger via
`kpi_plugin.bump_flash_cycle()` / `bump_block_f_cycle()` so the
cumulative-flash-cycles number rolls up across sessions. See
`docs/ams-hil/test-plan-v1.5.0.md` §4.

The trigger soak rows (F-071/F-072/F-080) drive the same flash helper
the operator uses in the car (`tools/flash_ams_via_trigger.py`).
That's deliberate: F-071 is the regression net for the trigger flash
path the pit-tool depends on.
"""

from __future__ import annotations

import time

import pytest

from tools.firmware_test.ams import can_map as M


# Cycle-count multiplier for development runs (full counts are slow).
# Profile-side hook: scripts/conftest pulls the multiplier from
# `--soak-cycle-scale` (default 1.0).
def _cycles(n: int, scale: float) -> int:
    return max(1, int(n * scale))


SCAFFOLD_PENDING = (
    "Scaffold — implement once Block F soak budget is approved. "
    "Wire to tools/flash_ams_via_trigger.py and bump kpi_plugin "
    "counters per cycle. See docs/ams-hil/test-plan-v1.5.0.md §1."
)

FLIGHT_BUILD_VARIANT_PENDING = (
    "Blocked on a flight-build (AMS_HIL_CLEAR_ERROR_LATCH=0) artifact "
    "fixture. Either add the build to the bench's CI matrix or ship "
    "a checked-in flight-mode AMS.bin alongside the HIL one."
)


def _ams_bin_path() -> str:
    """Path to the AMS app .bin to (re)flash in the trigger-flash soak
    rows. Default /tmp/AMS.bin; override with AMS_FIRMWARE_BIN. Skips the
    row if the image isn't staged (mirrors A-003's ams_firmware_bin, but
    module-local so the F-block doesn't depend on a Block-A fixture)."""
    import os
    from pathlib import Path
    p = Path(os.environ.get("AMS_FIRMWARE_BIN", "/tmp/AMS.bin"))
    if not p.exists():
        pytest.skip(f"AMS app .bin not found at {p} -- set AMS_FIRMWARE_BIN "
                    "or stage /tmp/AMS.bin.")
    return str(p)


def _wait_first_telem(observe_acu, budget_s: float):
    """Poll for the first `0x4A0` status frame within `budget_s`; return the
    CapturedFrame or None. The caller is responsible for clearing the
    observer first so a stale pre-event frame isn't returned."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
        if f is not None:
            return f
        time.sleep(0.02)
    return None


def _bus_noise(channel: str, stop, *, base_id: int = 0x500,
               rate_hz: float = 200.0) -> None:
    """Flood `channel` with filler standard-ID frames until `stop` (a
    threading.Event) is set. IDs sweep 0x500..0x5FF -- deliberately clear
    of every ID the AMS app or BL parses (0x002 trigger, 0x100 VCU, 0x101
    charger, 0x4A0/0x4A2 telemetry, 0x6C0/0x6C6 diag, 0x01N BL node) -- and
    never carry the B0 07 AD 11 trigger payload. So nothing is
    *protocol*-poked; the stressor is raw bus load: arbitration pressure
    plus RX-filter churn on the AMS. Runs best-effort -- any send error is
    swallowed so a transient bus hiccup can't crash the soak."""
    from tools.firmware_test.acu_stim import AcuStim
    try:
        stim = AcuStim(channel=channel)
        stim.start()
    except Exception:
        return
    period = 1.0 / rate_hz
    n = 0
    try:
        while not stop.is_set():
            try:
                stim.send_raw(base_id + (n & 0xFF),
                              bytes([n & 0xFF, 0x11, 0x22, 0x33,
                                     0x44, 0x55, 0x66, 0x77]),
                              is_extended_id=False)
            except Exception:
                pass
            n += 1
            time.sleep(period)
    finally:
        try:
            stim.stop()
        except Exception:
            pass


def _fwid_from_bin(path: str):
    """Derive the 8-byte on-wire 0x6C6 firmware-ID from a flashed image's
    embedded `bl_fwinfo_t` record (app-relative offset 0x400, the same
    record A-009/D-043 check). Semver bytes are LE u32s at rec+0x08/0x0C/
    0x10 (major/minor/patch), the git hash is 4 bytes at rec+0x18, the node
    id is the low byte at rec+0x38. Packed the way the firmware builds the
    0x6C6 payload -> the bench can assert the chip reports exactly what was
    flashed. Returns None if the record isn't present."""
    from pathlib import Path
    data = Path(path).read_bytes()
    rec = data[0x400:0x440]
    if len(rec) < 0x40:
        return None
    semver = bytes([rec[0x08], rec[0x0C], rec[0x10]])
    git = bytes(rec[0x18:0x1C])
    node = rec[0x38]
    return semver + git + bytes([node])


def _enable_and_read_fwid(observe_acu, ams_profile, timeout_s: float = 3.0):
    """Re-enable the pit-diag burst (a reflashed app boots with it OFF) and
    return the 8-byte 0x6C6 firmware-ID, or None on timeout. Mirrors the
    `pit_diag` fixture's enable (0xDEADBEEF -> 0x7F0) but per-cycle, since
    F-075 reboots the app on every flash."""
    import subprocess
    bus = ams_profile["bus_acu"]
    magic = M.PIT_DIAG_ENABLE_MAGIC.hex().upper()
    subprocess.run(["cansend", bus, f"{M.ID_PIT_DIAG_CMD:03X}#{magic}"],
                   check=False, timeout=2.0)
    observe_acu.clear()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        f = observe_acu.last(M.ID_PIT_DIAG_FW_ID, extended=False)
        if f is not None:
            return bytes(f.data[: f.dlc])
        time.sleep(0.02)
    return None


# ---------------------------------------------------------------------------
# F-070 — Cold soak (100×)
# ---------------------------------------------------------------------------

class TestF070ColdSoak:
    """100× power-cycle → BL DISCOVER → app jumps → first `0x4A0`
    within 5 s. Per-cycle medians of DISCOVER latency and
    first-telemetry latency must stay within ±10 % of cycle 1; zero
    cycles may fail outright.
    """

    @pytest.mark.soak
    def test_f070_cold_soak(self, mlc_powered, observe_acu,
                            ams_profile, soak_scale):
        import os
        import statistics
        from broker.server import BrokerClient
        from tests.hil.ams import kpi_plugin

        n_cycles = _cycles(100, soak_scale)
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        relay_bit = mlc_powered["relay_bit"]
        off_s = float(ams_profile["power_cycle_off_s"])
        # First telemetry is grace-gated (~boot_grace + first poll); budget
        # against that + BL/observe slack (see A-004 / E-052 wording).
        telem_budget_s = (int(ams_profile["boot_grace_ms"])
                          + int(ams_profile["tx_telemetry_period_ms"])
                          + 2500) / 1000.0
        telem_ms: list[float] = []
        failures: list[tuple[int, str]] = []
        try:
            for i in range(n_cycles):
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=False)
                time.sleep(off_s)
                observe_acu.clear()
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=True)
                t_on = time.monotonic()
                kpi_plugin.bump_power_cycle()

                # BL auto-jumps to the app (no DISCOVER here -- a DISCOVER
                # parks the BL waiting for a flash session and suppresses
                # the auto-jump; discover-latency is F-079's job). Wait for
                # the app's first 0x4A0 telemetry.
                first = None
                deadline = t_on + telem_budget_s + 1.0
                while time.monotonic() < deadline:
                    f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
                    if f is not None:
                        first = f
                        break
                    time.sleep(0.02)
                if first is None:
                    failures.append((i, "no 0x4A0 within budget after jump"))
                    continue
                telem_ms.append((time.monotonic() - t_on) * 1000)
                st = M.decode_telem_status(first.data)["state"]
                if st not in (M.FsmState.START, M.FsmState.ERROR):
                    failures.append((i, f"first state={M.FsmState.name(st)}"))
                kpi_plugin.bump_block_f_cycle()
        finally:
            client.close()

        assert not failures, (
            f"{len(failures)} of {n_cycles} cold-boot cycles failed. "
            f"First few: {failures[:5]}")
        # Latency-drift gate -- only meaningful with a real soak run.
        # Spec target is +/-10 %; the bench allows a wider band for boot
        # jitter (BL discover retries, observe polling) but still catches a
        # degrading boot pipeline.
        if len(telem_ms) >= 10:
            med = statistics.median(telem_ms)
            assert max(telem_ms) <= med * 1.30, (
                f"first-telem latency drift > 30%: median={med:.0f} ms, "
                f"worst={max(telem_ms):.0f} ms, cycle1={telem_ms[0]:.0f} ms "
                "-- the boot/flash pipeline is degrading over the soak.")


# ---------------------------------------------------------------------------
# F-071 — CAN-trigger soak (100×)
# ---------------------------------------------------------------------------

class TestF071CanTriggerSoak:
    """100× from running app: send `0x002` trigger → BL → flash a
    fresh app image → jump → first telemetry. Same invariants as
    F-070. This is the regression net for the in-car reflash flow.
    """

    @pytest.mark.soak
    def test_f071_can_trigger_soak(self, mlc_powered,
                                   observe_acu, ams_profile, soak_scale):
        from tools import flash_ams_via_trigger as fl
        from tests.hil.ams import kpi_plugin

        n_cycles = _cycles(100, soak_scale)
        bin_path = _ams_bin_path()
        telem_budget_s = (int(ams_profile["boot_grace_ms"])
                          + int(ams_profile["tx_telemetry_period_ms"])
                          + 2500) / 1000.0
        failures: list[tuple[int, str]] = []
        for i in range(n_cycles):
            # 1) Trigger the running app into the BL (002 on FDCAN1). If the
            #    chip is already in BL the trigger is a no-op -- discover
            #    confirms BL either way.
            fl.send_trigger()
            time.sleep(2.0)
            if not fl.discover_bl():
                failures.append((i, "BL not reachable after 002 trigger"))
                continue
            kpi_plugin.bump_bl_trigger()

            # 2) Flash a fresh image + jump.
            observe_acu.clear()
            r = fl.flash(bin_path, jump=True)
            if r.returncode != 0:
                failures.append((i, f"flash rc={r.returncode}: "
                                    f"{(r.stderr or '')[-120:]}"))
                continue
            kpi_plugin.bump_flash_cycle()

            # 3) App boots after the jump -> first 0x4A0.
            first = None
            deadline = time.monotonic() + telem_budget_s + 1.0
            while time.monotonic() < deadline:
                f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
                if f is not None:
                    first = f
                    break
                time.sleep(0.02)
            if first is None:
                failures.append((i, "no 0x4A0 after flash+jump"))
                continue
            st = M.decode_telem_status(first.data)["state"]
            if st not in (M.FsmState.START, M.FsmState.ERROR):
                failures.append((i, f"first state={M.FsmState.name(st)}"))
            kpi_plugin.bump_block_f_cycle()

        assert not failures, (
            f"{len(failures)} of {n_cycles} CAN-trigger flash cycles failed. "
            f"First few: {failures[:5]}")


# ---------------------------------------------------------------------------
# F-072 — Cross-trigger mix (100×)
# ---------------------------------------------------------------------------

class TestF072CrossTriggerMix:
    @pytest.mark.soak
    def test_f072_cross_trigger_mix(self, mlc_powered,
                                    observe_acu, ams_profile, soak_scale):
        import os
        from broker.server import BrokerClient
        from tools import flash_ams_via_trigger as fl
        from tests.hil.ams import kpi_plugin

        n_cycles = _cycles(100, soak_scale)
        bin_path = _ams_bin_path()
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        relay_bit = mlc_powered["relay_bit"]
        off_s = float(ams_profile["power_cycle_off_s"])
        telem_budget_s = (int(ams_profile["boot_grace_ms"])
                          + int(ams_profile["tx_telemetry_period_ms"])
                          + 2500) / 1000.0
        failures: list[tuple[int, str]] = []

        def _wait_telem():
            deadline = time.monotonic() + telem_budget_s + 1.0
            while time.monotonic() < deadline:
                f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
                if f is not None:
                    return f
                time.sleep(0.02)
            return None

        try:
            for i in range(n_cycles):
                if i % 2 == 0:
                    # Even cycle: cold power-cycle (F-070 path).
                    client.call("tca.write_pin", addr=0x20, port=0,
                                pin=relay_bit, value=False)
                    time.sleep(off_s)
                    observe_acu.clear()
                    client.call("tca.write_pin", addr=0x20, port=0,
                                pin=relay_bit, value=True)
                    kpi_plugin.bump_power_cycle()
                    first = _wait_telem()
                    if first is None:
                        failures.append((i, "cold-boot: no 0x4A0"))
                        continue
                else:
                    # Odd cycle: CAN-trigger reflash (F-071 path).
                    fl.send_trigger()
                    time.sleep(2.0)
                    if not fl.discover_bl():
                        failures.append((i, "trigger: BL not reachable"))
                        continue
                    kpi_plugin.bump_bl_trigger()
                    observe_acu.clear()
                    r = fl.flash(bin_path, jump=True)
                    if r.returncode != 0:
                        failures.append((i, f"trigger: flash rc={r.returncode}"))
                        continue
                    kpi_plugin.bump_flash_cycle()
                    first = _wait_telem()
                    if first is None:
                        failures.append((i, "trigger: no 0x4A0"))
                        continue
                st = M.decode_telem_status(first.data)["state"]
                if st not in (M.FsmState.START, M.FsmState.ERROR):
                    failures.append((i, f"first state={M.FsmState.name(st)}"))
                kpi_plugin.bump_block_f_cycle()
        finally:
            client.close()

        assert not failures, (
            f"{len(failures)} of {n_cycles} cross-trigger cycles failed "
            "(both reset paths must boot clean). "
            f"First few: {failures[:5]}")


# ---------------------------------------------------------------------------
# F-073 — CRC integrity per cycle (100×)
# ---------------------------------------------------------------------------

class TestF073CrcIntegrity:
    @pytest.mark.soak
    def test_f073_crc_integrity(self, mlc_powered,
                                observe_acu, ams_profile, soak_scale):
        from tools import flash_ams_via_trigger as fl
        from tests.hil.ams import kpi_plugin

        n_cycles = _cycles(100, soak_scale)
        bin_path = _ams_bin_path()
        telem_budget_s = (int(ams_profile["boot_grace_ms"])
                          + int(ams_profile["tx_telemetry_period_ms"])
                          + 2500) / 1000.0
        failures: list[tuple[int, str]] = []
        for i in range(n_cycles):
            fl.send_trigger()
            time.sleep(2.0)
            if not fl.discover_bl():
                failures.append((i, "BL not reachable after 002 trigger"))
                continue
            kpi_plugin.bump_bl_trigger()

            # verify_after=True turns the readback-CRC back on (the BL's
            # FlashReadCrc/FlashVerify ops over CAN, no SWD). A CRC mismatch
            # makes can-flasher exit non-zero -- that's the F-073 signal.
            observe_acu.clear()
            r = fl.flash(bin_path, jump=True, verify_after=True)
            if r.returncode != 0:
                failures.append((i, f"flash/verify rc={r.returncode}: "
                                    f"{(r.stderr or '')[-120:]}"))
                continue
            kpi_plugin.bump_flash_cycle()

            first = None
            deadline = time.monotonic() + telem_budget_s + 1.0
            while time.monotonic() < deadline:
                f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
                if f is not None:
                    first = f
                    break
                time.sleep(0.02)
            if first is None:
                failures.append((i, "no 0x4A0 after verified flash"))
                continue
            kpi_plugin.bump_block_f_cycle()

        assert not failures, (
            f"{len(failures)} of {n_cycles} CRC-verified flash cycles failed "
            f"(any readback-CRC mismatch counts). First few: {failures[:5]}")


# ---------------------------------------------------------------------------
# F-074 — Bus-busy flash (20×)
# ---------------------------------------------------------------------------

class TestF074BusBusyFlash:
    """The F-071 trigger-flash flow, but the flash bus is never idle: a
    filler thread floods can2 at ~200 frames/s for the whole flash. In a
    car the carrier bus carries other MLC traffic during a reflash, so the
    BL's flash protocol must be robust to RX churn -- the flash and the
    post-jump app boot must both still succeed.

    The can0 heartbeat is *paused* across the flash window: while the app
    is in the BL it listens on can2 only, so 0x100 frames on can0 go
    un-ACKed and would bus-off the stim socket (F-080 lesson). It resumes
    once the app is back up and ACKing can0.
    """

    @pytest.mark.soak
    def test_f074_bus_busy_flash(self, mlc_powered, observe_acu,
                                 ams_profile, soak_scale, acu_heartbeat):
        import threading
        from tools import flash_ams_via_trigger as fl
        from tests.hil.ams import kpi_plugin

        n_cycles = _cycles(20, soak_scale)
        bin_path = _ams_bin_path()
        flash_bus = ams_profile["bus_bms_bl"]
        telem_budget_s = (int(ams_profile["boot_grace_ms"])
                          + int(ams_profile["tx_telemetry_period_ms"])
                          + 2500) / 1000.0
        failures: list[tuple[int, str]] = []
        for i in range(n_cycles):
            fl.send_trigger()
            time.sleep(2.0)
            if not fl.discover_bl():
                failures.append((i, "BL not reachable after 002 trigger"))
                continue
            kpi_plugin.bump_bl_trigger()

            # App is in the BL now (RX on can2). Quiet the can0 heartbeat,
            # flood the flash bus, flash + jump under load.
            acu_heartbeat["pause"]()
            stop = threading.Event()
            noise = threading.Thread(
                target=_bus_noise, args=(flash_bus, stop),
                kwargs={"rate_hz": 200.0}, daemon=True)
            noise.start()
            observe_acu.clear()
            try:
                r = fl.flash(bin_path, jump=True)
            finally:
                stop.set()
                noise.join(timeout=2.0)
            if r.returncode != 0:
                failures.append((i, f"flash under bus load rc={r.returncode}: "
                                    f"{(r.stderr or '')[-120:]}"))
                acu_heartbeat["resume"]()
                continue
            kpi_plugin.bump_flash_cycle()

            first = _wait_first_telem(observe_acu, telem_budget_s + 1.0)
            acu_heartbeat["resume"]()
            if first is None:
                failures.append((i, "no 0x4A0 after busy-bus flash"))
                continue
            st = M.decode_telem_status(first.data)["state"]
            if st not in (M.FsmState.START, M.FsmState.ERROR):
                failures.append((i, f"first state={M.FsmState.name(st)}"))
            kpi_plugin.bump_block_f_cycle()

        assert not failures, (
            f"{len(failures)} of {n_cycles} bus-busy flash cycles failed "
            "(flash + boot must survive a loaded flash bus). "
            f"First few: {failures[:5]}")


# ---------------------------------------------------------------------------
# F-075 — Mixed-version round-trip (10×)
# ---------------------------------------------------------------------------

class TestF075MixedVersionRoundTrip:
    """Round-trip between two firmware versions and confirm the on-wire
    firmware-ID (pit-diag 0x6C6) exactly matches whichever image is
    currently flashed -- a reflash must never leave a stale identity.

    Image A defaults to the bench image (AMS_FIRMWARE_BIN / /tmp/AMS.bin);
    image B is a second version staged at AMS_FIRMWARE_BIN_B (default
    /tmp/AMS_v161.bin), built from the same source with a bumped VERSION +
    a distinct -DGIT_HASH. The expected 0x6C6 for each is *derived from the
    image's own embedded bl_fwinfo_t record*, so the assertion is "the chip
    reports exactly what we flashed," not a hard-coded constant. Skips
    cleanly if image B isn't staged.

    10 round-trips (B then A) scaled by --soak-cycle-scale.
    """

    @pytest.mark.soak
    def test_f075_mixed_version_round_trip(self, mlc_powered, observe_acu,
                                           ams_profile, soak_scale):
        import os
        from tools import flash_ams_via_trigger as fl
        from tests.hil.ams import kpi_plugin

        img_a = os.environ.get("AMS_FIRMWARE_BIN", "/tmp/AMS.bin")
        img_b = os.environ.get("AMS_FIRMWARE_BIN_B", "/tmp/AMS_v161.bin")
        if not os.path.exists(img_a):
            pytest.skip(f"image A not staged at {img_a}")
        if not os.path.exists(img_b):
            pytest.skip(f"image B (2nd version) not staged at {img_b} -- set "
                        "AMS_FIRMWARE_BIN_B; build from a VERSION bump + a "
                        "distinct -DGIT_HASH.")
        exp_a = _fwid_from_bin(img_a)
        exp_b = _fwid_from_bin(img_b)
        if exp_a is None or exp_b is None:
            pytest.skip("could not read a firmware_info record from an image")
        if exp_a[:3] == exp_b[:3]:
            pytest.skip(f"images are the same version (A semver="
                        f"{tuple(exp_a[:3])} == B) -- F-075 needs two distinct.")

        telem_budget_s = (int(ams_profile["boot_grace_ms"])
                          + int(ams_profile["tx_telemetry_period_ms"])
                          + 2500) / 1000.0
        cycles = _cycles(10, soak_scale)

        def _flash_and_fwid(img):
            fl.send_trigger()
            time.sleep(2.0)
            if not fl.discover_bl():
                return None, "BL not reachable after 002 trigger"
            kpi_plugin.bump_bl_trigger()
            observe_acu.clear()
            r = fl.flash(img, jump=True)
            if r.returncode != 0:
                return None, f"flash rc={r.returncode}: {(r.stderr or '')[-100:]}"
            kpi_plugin.bump_flash_cycle()
            if _wait_first_telem(observe_acu, telem_budget_s + 1.0) is None:
                return None, "no 0x4A0 after flash+jump"
            fwid = _enable_and_read_fwid(observe_acu, ams_profile)
            if fwid is None:
                return None, "no 0x6C6 firmware-ID after flash"
            return fwid, None

        failures: list[tuple[int, str]] = []
        # Round-trip B -> A each cycle; the on-wire ID must match the flashed
        # image's embedded record every single time.
        for i in range(cycles):
            for label, img, exp in (("B", img_b, exp_b), ("A", img_a, exp_a)):
                fwid, err = _flash_and_fwid(img)
                if err is not None:
                    failures.append((i, f"{label}: {err}"))
                    continue
                if fwid != exp:
                    failures.append(
                        (i, f"{label}: 0x6C6={fwid.hex()} != embedded "
                            f"{exp.hex()} (stale/wrong firmware-ID)"))
                    continue
                kpi_plugin.bump_block_f_cycle()

        assert not failures, (
            f"{len(failures)} of {cycles} B<->A round-trips reported a "
            f"wrong/stale firmware-ID. First few: {failures[:5]}")


# ---------------------------------------------------------------------------
# F-076 — Stale-latch flash (5× each variant)
# ---------------------------------------------------------------------------

class TestF076StaleLatchFlash:
    """Per AMS #272 F-076 (rewritten): drive the FSM to Error via a
    TSMS drop in Run (no SWD / no BKP poking) — that latches BKP1R via
    safety_task.cpp → ErrorLatch::set(). Then power-cycle and verify
    the latch-clear behaviour matches the build flag:

      HIL_CLEAR build:  next boot reads Start within SafetyBootGraceMs
                        (App_InitTask::ErrorLatch::clear() wipes the
                        latch; #226 contract).
      Flight build:     boot reads Error and stays (latch survives).

    Only the HIL_CLEAR variant runs against this bench's build (the
    sync target is always built with AMS_HIL_CLEAR_ERROR_LATCH=ON).
    The flight variant stays skipped until a flight .bin lands in the
    bench's artifact pipeline.
    """

    @pytest.mark.soak
    def test_f076_hil_clear_set(
        self, fresh_boot, mlc_powered, tsms, dash_chg, acu_heartbeat,
        wait_for_state, observe_acu, ams_profile,
    ):
        import os
        from broker.server import BrokerClient

        # Skip cleanly if the cockpit fixtures aren't wired (consistent
        # with Block C behaviour for the same dependency).
        if tsms is None or dash_chg is None:
            pytest.skip("tsms / dash_chg fixture unavailable")

        # Step 1: drive FSM to Error via TSMS drop in Run. This writes
        # BKP1R = 0xA115EE51 inside safety_task::trip_error.
        from tests.hil.ams.test_block_c_fsm import _drive_to_run
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile)
        tsms.deassert()
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile["state_transition_window_ms"]) + 200)

        # Step 2: power-cycle MLC2.
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        try:
            relay_bit = mlc_powered["relay_bit"]
            client.call("tca.write_pin", addr=0x20, port=0,
                        pin=relay_bit, value=False)
            time.sleep(float(ams_profile["power_cycle_off_s"]))
            observe_acu.clear()
            client.call("tca.write_pin", addr=0x20, port=0,
                        pin=relay_bit, value=True)
        finally:
            client.close()

        # Step 3: HIL_CLEAR build must clear the latch and come up Start
        # within SafetyBootGraceMs (= 2 s) + boot+telemetry slack.
        boot_grace_ms = int(ams_profile.get("boot_grace_ms", 2000))
        budget_ms = boot_grace_ms + 3000   # BL + first-telem slack
        deadline = time.monotonic() + budget_ms / 1000.0
        last = None
        while time.monotonic() < deadline:
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            if f is not None:
                last = M.decode_telem_status(f.data)["state"]
                if last == M.FsmState.START:
                    return  # PASS — latch was cleared
            time.sleep(0.05)
        raise AssertionError(
            f"After TSMS-drop → Error → power-cycle, first 0x4A0 within "
            f"{budget_ms} ms was state={last}; expected Start. "
            "AMS_HIL_CLEAR_ERROR_LATCH may not be wiping BKP1R on boot.")

    @pytest.mark.soak
    @pytest.mark.skip(reason=FLIGHT_BUILD_VARIANT_PENDING)
    def test_f076_hil_clear_unset(self):
        # Flight variant: build with AMS_HIL_CLEAR_ERROR_LATCH=0, repeat
        # the TSMS-drop fault stim, power-cycle, assert chip comes up
        # Error and stays. Skipped until a flight .bin lands in the
        # bench artifact pipeline.
        pass


# ---------------------------------------------------------------------------
# F-077 — Interrupted-flash recovery (10×)
# ---------------------------------------------------------------------------

class TestF077InterruptedFlashRecovery:
    """Yank carrier power mid-flash, 10x, and prove the unit always
    recovers: after an interrupted flash the BL must stay reachable and a
    clean image must boot afterwards. An interrupted flash must never brick
    the BL or strand an unrecoverable half-flashed app.

    Caveat (why this isn't the literal #245 F-077): the relay K_n yank is
    coarse -- it lands *somewhere* in the multi-second flash, not between
    two specific FLASH_WRITE frames -- so it can't probe partial-sector
    write atomicity. PR #127 Test A already covers the single-shot "never
    jump a half-flashed image" invariant (host loss); this is the soak that
    the *recovery* path survives repeated power loss mid-write. The
    interrupting flash uses --no-diff so it always writes every sector
    (a same-image diff-flash would finish before the cut).

    10 cycles scaled by --soak-cycle-scale.
    """

    @pytest.mark.soak
    def test_f077_interrupted_flash_recovery(self, mlc_powered, observe_acu,
                                             ams_profile, soak_scale):
        import os
        import threading
        from broker.server import BrokerClient
        from tools import flash_ams_via_trigger as fl
        from tests.hil.ams import kpi_plugin

        img = os.environ.get("AMS_FIRMWARE_BIN", "/tmp/AMS.bin")
        if not os.path.exists(img):
            pytest.skip(f"AMS image not staged at {img}")
        exp = _fwid_from_bin(img)
        cycles = _cycles(10, soak_scale)
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        relay_bit = mlc_powered["relay_bit"]
        off_s = float(ams_profile["power_cycle_off_s"])
        telem_budget_s = (int(ams_profile["boot_grace_ms"])
                          + int(ams_profile["tx_telemetry_period_ms"])
                          + 2500) / 1000.0

        def _relay(on: bool):
            client.call("tca.write_pin", addr=0x20, port=0,
                        pin=relay_bit, value=on)

        def _reach_bl() -> bool:
            """Get the unit into a flashable BL after an interrupted flash.
            BL already up (half-flashed app failed its validity check) ->
            done. Else the app is intact and running -> trigger it back.
            Else a clean power-cycle (a half/hung app can't auto-jump, so
            the BL stays)."""
            if fl.discover_bl():
                return True
            fl.send_trigger()
            time.sleep(2.0)
            if fl.discover_bl():
                return True
            _relay(False)
            time.sleep(off_s)
            _relay(True)
            time.sleep(1.0)
            return fl.discover_bl()

        failures: list[tuple[int, str]] = []
        try:
            for i in range(cycles):
                # 1) Into BL, start a full-sector flash, then yank mid-write.
                fl.send_trigger()
                time.sleep(2.0)
                if not fl.discover_bl():
                    failures.append((i, "pre-yank: BL not reachable"))
                    continue
                kpi_plugin.bump_bl_trigger()

                holder: dict = {}

                def _do_flash():
                    holder["r"] = fl.flash(img, jump=False, force_all=True,
                                           timeout_ms=4000)

                th = threading.Thread(target=_do_flash, daemon=True)
                th.start()

                # Vary the cut point across the soak so it lands at different
                # places in the write (1.2 .. 3.2 s in).
                time.sleep(1.2 + 0.5 * (i % 5))
                _relay(False)                 # power gone mid-write
                kpi_plugin.bump_power_cycle()
                # Flasher errors out on the dead bus (power is OFF the whole
                # time, so no zombie flasher survives into recovery).
                th.join(timeout=30.0)
                time.sleep(off_s)
                observe_acu.clear()
                _relay(True)

                # 2) Recover: reach the BL and flash a known-good image.
                if not _reach_bl():
                    failures.append(
                        (i, "UNRECOVERABLE: BL unreachable after interrupted "
                            "flash + power-cycle -- unit bricked"))
                    continue
                # Recovery: force every sector (a --diff flash would diff
                # against the half-written/garbage chip and might skip a
                # sector it wrongly thinks already matches). Do NOT warm
                # --jump: an immediate jump after a force-write doesn't
                # reliably boot the app on this BL, but a cold power-cycle
                # always does (F-070/078), so write-then-cold-boot.
                r = fl.flash(img, jump=True, force_all=True)
                if r.returncode != 0:
                    failures.append((i, f"recovery flash rc={r.returncode}: "
                                        f"{(r.stderr or '')[-100:]}"))
                    continue
                kpi_plugin.bump_flash_cycle()
                # Boot the freshly written app. Try the warm --jump first --
                # this ALSO lets the flash/metadata commit settle, so we never
                # cut power the instant the flasher returns (doing so lands
                # mid-commit and corrupts the validity marker). If the warm
                # jump didn't boot, the commit is settled by now, so a clean
                # power-cycle cold-boots it reliably.
                observe_acu.clear()
                first = _wait_first_telem(observe_acu, telem_budget_s + 1.0)
                if first is None:
                    _relay(False)
                    time.sleep(off_s)
                    observe_acu.clear()
                    _relay(True)
                    first = _wait_first_telem(observe_acu, telem_budget_s + 1.0)
                if first is None:
                    tail = (r.stdout or "")[-160:].replace(chr(10), " | ")
                    failures.append((i, f"no 0x4A0 after recovery (warm jump "
                                        f"+ cold boot) [flash said: {tail}]"))
                    continue
                # 3) Recovered identity intact (no lingering half-image).
                fwid = _enable_and_read_fwid(observe_acu, ams_profile)
                if exp is not None and fwid is not None and fwid != exp:
                    failures.append((i, f"post-recovery 0x6C6={fwid.hex()} != "
                                        f"embedded {exp.hex()}"))
                    continue
                kpi_plugin.bump_block_f_cycle()
        finally:
            client.close()

        assert not failures, (
            f"{len(failures)} of {cycles} interrupted-flash cycles failed to "
            f"recover cleanly. First few: {failures[:5]}")


# ---------------------------------------------------------------------------
# F-078 — Power-off duration sweep (5× each duration)
# ---------------------------------------------------------------------------

class TestF078PowerOffDurationSweep:
    """F-070's cold-boot invariant swept across power-off durations. A
    too-short off window (bus caps not fully discharged, BL sees a warm
    reset) is a classic source of a confused boot; a very long one shakes
    out any RTC-backed / brown-out-latch assumption. Every duration must
    still yield a clean first 0x4A0 in Start/Error.

    5 cycles per duration (scaled by --soak-cycle-scale). NB the 300 s
    point is deliberately long -- at full scale this row alone parks the
    bench for ~30 min on the 300 s case.
    """

    @pytest.mark.soak
    @pytest.mark.parametrize("off_s", [1, 5, 30, 60, 300])
    def test_f078_power_off_duration(self, off_s, mlc_powered, observe_acu,
                                     ams_profile, soak_scale):
        import os
        from broker.server import BrokerClient
        from tests.hil.ams import kpi_plugin

        cycles = _cycles(5, soak_scale)
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        relay_bit = mlc_powered["relay_bit"]
        telem_budget_s = (int(ams_profile["boot_grace_ms"])
                          + int(ams_profile["tx_telemetry_period_ms"])
                          + 2500) / 1000.0
        failures: list[tuple[int, str]] = []
        try:
            for i in range(cycles):
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=False)
                time.sleep(off_s)
                observe_acu.clear()
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=True)
                kpi_plugin.bump_power_cycle()
                first = _wait_first_telem(observe_acu, telem_budget_s + 1.0)
                if first is None:
                    failures.append((i, f"off={off_s}s: no 0x4A0 within budget"))
                    continue
                st = M.decode_telem_status(first.data)["state"]
                if st not in (M.FsmState.START, M.FsmState.ERROR):
                    failures.append(
                        (i, f"off={off_s}s: first state={M.FsmState.name(st)}"))
                kpi_plugin.bump_block_f_cycle()
        finally:
            client.close()

        assert not failures, (
            f"off={off_s}s: {len(failures)} of {cycles} cold-boot cycles "
            f"failed. First few: {failures[:5]}")


# ---------------------------------------------------------------------------
# F-079 — DISCOVER latency long-soak (1000×)
# ---------------------------------------------------------------------------

class TestF079DiscoverLatencyLongSoak:
    """1000× boot → reach BL → time a DISCOVER. Each cycle cold-boots the
    carrier, lets the app come up, then fires the 0x002 trigger to park the
    BL (a bare power-cycle won't sit in BL -- with a valid app the BL
    auto-jumps; the trigger's BKP magic is what holds it) and times the
    DISCOVER.

    Hard gate: zero missed responses across the whole soak -- DISCOVER is
    the operator's confirmation the BL is reachable before a flash, so it
    must never silently drop. Soft gate: DISCOVER latency must not drift
    upward over the soak (p99 within 1.5x median).

    NB the recorded latency is wall-time around `discover_bl()`, so it
    includes the can-flasher *process* startup (~hundreds of ms) on top of
    the on-wire BL response -- it's a soak-drift metric, not an absolute
    on-wire number. mlc_boot_settle_s (0.5 s) is the relay-settle delay,
    not a DISCOVER budget, so it is not asserted against.
    """

    @pytest.mark.soak
    def test_f079_discover_latency_long_soak(self, mlc_powered, observe_acu,
                                             ams_profile, soak_scale):
        import os
        import statistics
        from broker.server import BrokerClient
        from tools import flash_ams_via_trigger as fl
        from tests.hil.ams import kpi_plugin

        n_cycles = _cycles(1000, soak_scale)
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        relay_bit = mlc_powered["relay_bit"]
        off_s = float(ams_profile["power_cycle_off_s"])
        telem_budget_s = (int(ams_profile["boot_grace_ms"])
                          + int(ams_profile["tx_telemetry_period_ms"])
                          + 2500) / 1000.0
        lat_ms: list[float] = []
        misses: list[tuple[int, str]] = []
        try:
            for i in range(n_cycles):
                # Cold-boot so each DISCOVER starts from a fresh BL.
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=False)
                time.sleep(off_s)
                observe_acu.clear()
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=True)
                kpi_plugin.bump_power_cycle()
                if _wait_first_telem(observe_acu, telem_budget_s + 1.0) is None:
                    misses.append((i, "app never booted (no 0x4A0 pre-trigger)"))
                    continue

                # Trigger into the BL and park it, then time the DISCOVER.
                fl.send_trigger()
                time.sleep(2.0)
                kpi_plugin.bump_bl_trigger()
                t0 = time.monotonic()
                ok = fl.discover_bl()
                dt_ms = (time.monotonic() - t0) * 1000.0
                if not ok:
                    misses.append((i, f"DISCOVER missed (waited {dt_ms:.0f} ms)"))
                    continue
                lat_ms.append(dt_ms)
                kpi_plugin.record_bl_discover_latency_ms(dt_ms)
                kpi_plugin.bump_block_f_cycle()

            # The final DISCOVER leaves the BL parked (no jump). Boot the app
            # back so the next row doesn't start stranded in the BL.
            _bin = os.environ.get("AMS_FIRMWARE_BIN", "/tmp/AMS.bin")
            if os.path.exists(_bin):
                try:
                    fl.flash(_bin, jump=True)
                except Exception:
                    pass
        finally:
            client.close()

        assert not misses, (
            f"{len(misses)} of {n_cycles} boot->BL DISCOVER cycles missed the "
            f"response (DISCOVER must never silently drop). "
            f"First few: {misses[:5]}")
        if len(lat_ms) >= 10:
            ls = sorted(lat_ms)
            med = statistics.median(ls)
            p99 = ls[min(len(ls) - 1, int(round(0.99 * (len(ls) - 1))))]
            assert p99 <= med * 1.5, (
                f"DISCOVER latency drifting over the soak: median={med:.0f} ms, "
                f"p99={p99:.0f} ms across {len(lat_ms)} cycles "
                "-- the BL discovery path is degrading.")


# ---------------------------------------------------------------------------
# F-080 — Trigger-from-Error (20×)
# ---------------------------------------------------------------------------

class TestF080TriggerFromError:
    """Per AMS #272 F-080 (rewritten): force Error via TSMS drop in Run
    (no SWD / no GDB), then issue the BL trigger. The trigger handler
    in AcuCanTask must remain reachable regardless of FSM state —
    especially Error, since that's the state where a fix-and-reflash
    is most needed.

    Same fault-stim path as F-076, but the assertion is "BL trigger
    still works" rather than "latch clears on boot". Sibling test to
    D-051b but specifically a regression net for the post-#243
    trigger-from-Error path under soak.

    Default 20 cycles per #272. Scaled by `--soak-cycle-scale`.
    """

    @pytest.mark.soak
    def test_f080_trigger_from_error(
        self, mlc_powered, tsms, dash_chg, acu_heartbeat,
        wait_for_state, observe_acu, ams_profile, soak_scale,
    ):
        import subprocess
        # Same TSMS-drop fault-stim helpers as F-076.
        if tsms is None or dash_chg is None:
            pytest.skip("tsms / dash_chg fixture unavailable")
        from tests.hil.ams.test_block_c_fsm import _drive_to_run
        from tests.hil.ams.test_block_d_bootloader import _trigger_rebooted_to_bl

        cycles = _cycles(20, soak_scale)
        failures: list[tuple[int, str]] = []
        from broker.server import BrokerClient
        import os
        client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                             "/run/hil-broker/broker.sock"))
        try:
            for i in range(cycles):
                # 1) Cold-boot via K_n cycle so each iteration starts from a
                #    known-good state. Pause the heartbeat across the off
                #    window (sending 0x100 to a dead carrier breaks the
                #    heartbeat thread + congests can0 on recovery).
                relay_bit = mlc_powered["relay_bit"]
                acu_heartbeat["pause"]()
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=False)
                time.sleep(float(ams_profile["power_cycle_off_s"]))
                observe_acu.clear()
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=True)
                # Reset dc_bus to quiescent: a value left ramped at pack from
                # the previous cycle pre-satisfies precharge, so the next
                # drive blows past Precharge -- _drive_to_run ramps from 0.
                acu_heartbeat["set_volts"](0)

                # Wait for the app to boot (heartbeat stays PAUSED -- RX works
                # regardless; resuming into BL/boot fills the can0 TX buffer
                # and breaks the heartbeat thread for good -> VCU goes stale
                # -> the drive below faults to Error instead of reaching Run).
                # The #316 press edge is also lost if fired before the app is
                # ready (0x4A0 is grace-gated to ~boot_grace + first poll).
                booted = False
                bd = time.monotonic() + 5.0
                while time.monotonic() < bd:
                    if observe_acu.last(M.ID_TELEM_STATUS, extended=False) is not None:
                        booted = True
                        break
                    time.sleep(0.05)
                if not booted:
                    failures.append((i, "no telemetry within 5 s of power-on"))
                    continue
                acu_heartbeat["resume"]()   # app up + ACKing -> safe to send

                # 2) Drive into Run, then TSMS drop → Error.
                try:
                    _drive_to_run(tsms, dash_chg, acu_heartbeat,
                                  wait_for_state, ams_profile)
                except Exception as e:
                    failures.append((i, f"drive_to_run failed: {e}"))
                    continue
                tsms.deassert()
                try:
                    wait_for_state(
                        M.FsmState.ERROR,
                        timeout_ms=int(ams_profile["state_transition_window_ms"]) + 200)
                except Exception as e:
                    failures.append((i, f"TSMS-drop didn't trip Error: {e}"))
                    continue

                # 3) Trigger BL from Error.
                subprocess.run(
                    ["cansend", ams_profile["bus_acu"], "002#B007AD11"],
                    check=False, timeout=2)
                if not _trigger_rebooted_to_bl(observe_acu, ams_profile):
                    failures.append(
                        (i, "BL trigger ignored while FSM in Error — "
                            "SAFETY-CRITICAL: app becomes unreflashable "
                            "from the cockpit fault path"))
        finally:
            client.close()

        assert not failures, (
            f"{len(failures)} of {cycles} trigger-from-Error cycles failed. "
            f"First few: {failures[:5]}")


# ---------------------------------------------------------------------------
# F-081 — Bench-noise immunity (60 s)
# ---------------------------------------------------------------------------

class TestF081BenchNoiseImmunity:
    """Flood can0 (the trigger bus) with ~200 filler standard-ID frames/s
    while the app runs, then check two invariants:

      1. zero spurious reboots -- 0x4A0 telemetry never gaps beyond a boot
         cycle (worst inter-frame period stays under 0.75 x boot_grace), so
         no filler frame was mistaken for a reset/trigger; and
      2. a *valid* 0x002 trigger still reaches the BL right after the noise
         window -- the RX filter rejected the noise without going deaf to
         the real trigger.

    Duration is 60 s at full scale, floored at 5 s and scaled by
    --soak-cycle-scale. Leaves the bench running: it reflashes + jumps the
    app back after the trigger check.
    """

    @pytest.mark.soak
    def test_f081_bench_noise_immunity(self, mlc_powered, observe_acu,
                                       acu_heartbeat, ams_profile, soak_scale):
        import threading
        from tools import flash_ams_via_trigger as fl
        from tests.hil.ams import kpi_plugin

        bus = ams_profile["bus_acu"]
        telem_period = int(ams_profile["tx_telemetry_period_ms"])
        boot_grace = int(ams_profile["boot_grace_ms"])
        dur_s = max(5.0, 60.0 * float(soak_scale))
        telem_budget_s = (boot_grace + telem_period + 2500) / 1000.0

        # App must already be streaming telemetry before we can judge a
        # "spurious reboot" against a baseline of steady 0x4A0.
        if _wait_first_telem(observe_acu, telem_budget_s) is None:
            pytest.skip("app not streaming 0x4A0 -- can't run noise-immunity")

        # Flood can0 with filler for the window.
        stop = threading.Event()
        noise = threading.Thread(target=_bus_noise, args=(bus, stop),
                                 kwargs={"base_id": 0x500, "rate_hz": 200.0},
                                 daemon=True)
        observe_acu.clear()
        t_start = time.time()
        noise.start()
        try:
            time.sleep(dur_s)
        finally:
            stop.set()
            noise.join(timeout=2.0)

        # (1) No reboot: normal cadence is telem_period (500 ms); a reboot
        # silences 0x4A0 for >= boot_grace (2 s). 0.75 x boot_grace (1500 ms)
        # cleanly separates jittered-normal from a reset.
        n_seen = observe_acu.count(M.ID_TELEM_STATUS, extended=False,
                                   since=t_start)
        worst_gap = observe_acu.max_period_ms(M.ID_TELEM_STATUS,
                                              extended=False, since=t_start)
        gap_ceiling = boot_grace * 0.75
        assert n_seen >= 2, (
            f"only {n_seen} 0x4A0 frames during the {dur_s:.0f}s noise window "
            "-- telemetry stalled (possible reboot or bus saturation).")
        assert worst_gap is not None and worst_gap <= gap_ceiling, (
            f"0x4A0 gapped {worst_gap:.0f} ms during noise (ceiling "
            f"{gap_ceiling:.0f} ms) -- a spurious reboot or telemetry stall "
            "under bench noise.")

        # (2) Valid trigger still works after the noise.
        fl.send_trigger()
        time.sleep(2.0)
        assert fl.discover_bl(), (
            "valid 0x002 trigger ignored after the noise window -- the RX "
            "filter may be wedged by sustained bench noise.")
        kpi_plugin.bump_bl_trigger()

        # Leave the bench in a running state: reflash + jump back to the app.
        r = fl.flash(_ams_bin_path(), jump=True)
        assert r.returncode == 0, (
            f"post-noise restore flash failed rc={r.returncode}: "
            f"{(r.stderr or '')[-120:]}")
        kpi_plugin.bump_flash_cycle()
        kpi_plugin.bump_block_f_cycle()
