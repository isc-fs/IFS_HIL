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
| F-073 | CRC integrity per cycle (READ_VERIFY)              | 100    | scaffolded — needs BL READ_VERIFY or SWD |
| F-074 | Bus-busy flash (heartbeat + noise during flash)    |  20    | scaffolded  |
| F-075 | Mixed-version round-trip (v1.5 → v1.4-eol → v1.5)  |  10    | scaffolded — needs v1.4-eol image fixture |
| F-076 | Stale-latch flash (with/without HIL_CLEAR)         | 5×2    | scaffolded — needs SWD to pre-set BKP1R |
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

BL_READ_VERIFY_PENDING = (
    "Blocked on BL READ_VERIFY support over CAN (or SWD attach on "
    "the bench). Track in the stm32-can-bootloader repo."
)

V1_4_EOL_IMAGE_PENDING = (
    "Blocked on a checked-in v1.4.0-eol .bin fixture under "
    "tests/hil/ams/fixtures/. Add once the AMS team publishes a "
    "frozen v1.4 image."
)

SWD_PRE_SET_BKP_PENDING = (
    "Blocked on a pre-test SWD step that writes RTC->BKP1R = "
    "0xA115EE51 before the boot cycle. Either ST-Link CLI scripted "
    "into the bench fixture, or extend the BL with a diag-write op."
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
    @pytest.mark.skip(reason=BL_READ_VERIFY_PENDING)
    def test_f073_crc_integrity(self):
        # Sketch: after each F-071 cycle, read back
        # 0x08020000..0x080DFFFF via BL READ_VERIFY (or SWD), compute
        # CRC, compare against source. Zero mismatches over 100 cycles.
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
    @pytest.mark.soak
    @pytest.mark.skip(reason=SWD_PRE_SET_BKP_PENDING)
    def test_f076_hil_clear_set(self):
        # Pre-set RTC->BKP1R = 0xA115EE51 via SWD, run F-071 once
        # with AMS_HIL_CLEAR_ERROR_LATCH=1. App must boot out of Error.
        pass

    @pytest.mark.soak
    @pytest.mark.skip(reason=SWD_PRE_SET_BKP_PENDING + " " + FLIGHT_BUILD_VARIANT_PENDING)
    def test_f076_hil_clear_unset(self):
        # Same pre-set, but with HIL_CLEAR=0 (flight build). App must
        # boot INTO Error and the latch must survive.
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
    @pytest.mark.soak
    @pytest.mark.skip(reason=FLIGHT_BUILD_VARIANT_PENDING)
    def test_f080_trigger_from_error(self):
        # Sketch: HIL_CLEAR=0 build with the latch deliberately set,
        # repeat F-071 20 times. The trigger path must still reach BL
        # even though the app booted into Error. Regression net for
        # the post-#243 + post-D-041b path.
        pass


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
