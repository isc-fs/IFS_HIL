"""
Block F — Flash endurance.

Per `isc-fs/IFS08-CE-AMS#245`. Soak the entire reset / flash / boot
pipeline against the current state of #210 (PB9 move), #226
(standalone latch clear), and #243 (BL trigger silent-drop fix).
Acceptance target: 100 cycles for the soak rows, 10 cycles each for
the variant rows.

| Test  | What it checks                                     | Cycles | Status      |
|-------|----------------------------------------------------|--------|-------------|
| F-070 | Cold soak: power-cycle → BL → app jump → first 4A0 | 100    | scaffolded  |
| F-071 | CAN-trigger soak: trigger → BL → flash → jump      | 100    | scaffolded  |
| F-072 | Cross-trigger mix: alternate cold + CAN reboots    | 100    | scaffolded  |
| F-073 | CRC integrity per cycle (can-flasher readback-CRC) | 100    | scaffolded  |
| F-074 | Bus-busy flash (heartbeat + noise during flash)    |  20    | scaffolded  |
| F-075 | Mixed-version round-trip (v1.5 → v1.4-eol → v1.5)  |  10    | scaffolded — needs v1.4-eol image fixture |
| F-076 | Stale-latch flash (TSMS-drop fault stim, no SWD)   | 5×2    | HIL_CLEAR impl; flight variant needs flight build |
| F-077 | Interrupted-flash recovery (yank VBUS mid-flash)   |  10    | scaffolded — needs programmable PSU or relay yank |
| F-078 | Power-off duration sweep ({1, 5, 30, 60, 300} s)   | 5×5    | scaffolded  |
| F-079 | DISCOVER latency long-soak                         | 1000   | scaffolded  |
| F-080 | Trigger-from-Error (HIL_CLEAR=0)                   |  20    | scaffolded — needs flight build variant |
| F-081 | Bench-noise immunity (200 std-ID/s + valid trigger)|  60 s  | scaffolded  |

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

V1_4_EOL_IMAGE_PENDING = (
    "Blocked on a checked-in v1.4.0-eol .bin fixture under "
    "tests/hil/ams/fixtures/. Add once the AMS team publishes a "
    "frozen v1.4 image."
)

INTERRUPT_FLASH_PENDING = (
    "Blocked on programmable PSU control (TCA relay K_n alone is too "
    "coarse-grained -- need to yank between two FLASH_WRITE frames, "
    "not at sector boundaries) or a precise relay-yank fixture."
)

FLIGHT_BUILD_VARIANT_PENDING = (
    "Blocked on a flight-build (AMS_HIL_CLEAR_ERROR_LATCH=0) artifact "
    "fixture. Either add the build to the bench's CI matrix or ship "
    "a checked-in flight-mode AMS.bin alongside the HIL one."
)


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
    @pytest.mark.skip(reason=SCAFFOLD_PENDING)
    def test_f070_cold_soak(self):
        # Sketch:
        #   N = _cycles(100, soak_cycle_scale)
        #   for i in range(N):
        #       t0 = monotonic()
        #       mlc_powered.cycle()  -> kpi_plugin.bump_power_cycle()
        #       discover_latency_ms = flasher.discover_with_latency()
        #       kpi_plugin.record_bl_discover_latency_ms(...)
        #       wait_first_4A0()
        #       kpi_plugin.bump_block_f_cycle()
        #   assert per-cycle latency drift < 10 %
        pass


# ---------------------------------------------------------------------------
# F-071 — CAN-trigger soak (100×)
# ---------------------------------------------------------------------------

class TestF071CanTriggerSoak:
    """100× from running app: send `0x002` trigger → BL → flash a
    fresh app image → jump → first telemetry. Same invariants as
    F-070. This is the regression net for the in-car reflash flow.
    """

    @pytest.mark.soak
    @pytest.mark.skip(reason=SCAFFOLD_PENDING)
    def test_f071_can_trigger_soak(self):
        # Sketch: drives tools/flash_ams_via_trigger.py per cycle;
        # bumps kpi_plugin.bump_bl_trigger() + bump_flash_cycle() +
        # bump_block_f_cycle().
        pass


# ---------------------------------------------------------------------------
# F-072 — Cross-trigger mix (100×)
# ---------------------------------------------------------------------------

class TestF072CrossTriggerMix:
    @pytest.mark.soak
    @pytest.mark.skip(reason=SCAFFOLD_PENDING)
    def test_f072_cross_trigger_mix(self):
        # Sketch: alternate F-070 and F-071 over 100 cycles. Both
        # reset paths must produce a clean boot.
        pass


# ---------------------------------------------------------------------------
# F-073 — CRC integrity per cycle (100×)
# ---------------------------------------------------------------------------

class TestF073CrcIntegrity:
    @pytest.mark.soak
    @pytest.mark.skip(reason=SCAFFOLD_PENDING)
    def test_f073_crc_integrity(self):
        # After each F-071 cycle, CRC-compare 0x08020000..0x080DFFFF
        # against the source image via `can-flasher verify` (or
        # `flash --verify-after`, a readback-CRC over CAN through the
        # BL's FlashReadCrc/FlashVerify ops — no SWD). The trigger soak
        # flasher runs --no-verify-after for speed; F-073 is the row
        # that turns the readback-CRC back on. Zero mismatches / 100.
        pass


# ---------------------------------------------------------------------------
# F-074 — Bus-busy flash (20×)
# ---------------------------------------------------------------------------

class TestF074BusBusyFlash:
    @pytest.mark.soak
    @pytest.mark.skip(reason=SCAFFOLD_PENDING)
    def test_f074_bus_busy_flash(self):
        # Sketch: run F-071 while acu_heartbeat injects 0x100#6801 at
        # 20 Hz AND a noise thread sends 0x002-adjacent IDs. BL flash
        # + app boot must still succeed.
        pass


# ---------------------------------------------------------------------------
# F-075 — Mixed-version round-trip (10×)
# ---------------------------------------------------------------------------

class TestF075MixedVersionRoundTrip:
    @pytest.mark.soak
    @pytest.mark.skip(reason=V1_4_EOL_IMAGE_PENDING)
    def test_f075_mixed_version_round_trip(self):
        # Sketch: flash v1.5.0, verify firmware_info, flash v1.4.0-eol,
        # verify firmware_info, flash v1.5.0 again, verify. 10 round
        # trips. firmware_info.fw_version_* must read correctly each
        # time and the unit boots cleanly.
        pass


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
    @pytest.mark.soak
    @pytest.mark.skip(reason=INTERRUPT_FLASH_PENDING)
    def test_f077_interrupted_flash_recovery(self):
        # Sketch: yank VBUS between two FLASH_WRITE frames; power back
        # up; verify next BL cycle either re-flashes successfully or
        # reports NO_VALID_APP and stays in BL (NEVER jumps to a
        # half-flashed image).
        pass


# ---------------------------------------------------------------------------
# F-078 — Power-off duration sweep (5× each duration)
# ---------------------------------------------------------------------------

class TestF078PowerOffDurationSweep:
    @pytest.mark.soak
    @pytest.mark.parametrize("off_s", [1, 5, 30, 60, 300])
    @pytest.mark.skip(reason=SCAFFOLD_PENDING)
    def test_f078_power_off_duration(self, off_s):
        # Sketch: F-070 but with mlc_power_off_s varying. Post-#226
        # HIL build clears the latch every boot regardless of duration;
        # flight build retains it.
        pass


# ---------------------------------------------------------------------------
# F-079 — DISCOVER latency long-soak (1000×)
# ---------------------------------------------------------------------------

class TestF079DiscoverLatencyLongSoak:
    @pytest.mark.soak
    @pytest.mark.skip(reason=SCAFFOLD_PENDING)
    def test_f079_discover_latency_long_soak(self):
        # Sketch: 1000× BL DISCOVER (no app re-flash). Each cycle:
        # power-cycle, time DISCOVER, record_bl_discover_latency_ms.
        # Assert p99 < mlc_boot_settle_s, zero missed responses.
        pass


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
                # 1) Cold-boot via K_n cycle so each iteration starts
                #    from a known-good state.
                relay_bit = mlc_powered["relay_bit"]
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=False)
                time.sleep(float(ams_profile["power_cycle_off_s"]))
                client.call("tca.write_pin", addr=0x20, port=0,
                            pin=relay_bit, value=True)
                time.sleep(float(ams_profile["mlc_boot_settle_s"]))

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
    @pytest.mark.soak
    @pytest.mark.skip(reason=SCAFFOLD_PENDING)
    def test_f081_bench_noise_immunity(self):
        # Sketch: spawn a thread sending 200 random standard-ID
        # frames/s on can0 (none of which match the trigger payload).
        # Confirm: zero spurious reboots; valid trigger still works
        # at the end of the window.
        pass
