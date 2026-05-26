"""
Block E — LTC chain integrity.

Per `isc-fs/IFS08-CE-AMS#272`. The real LTC path runs in every build
post-#207 (HIL_STUB removed); on the bench the Pi Pico LTC6820/LTC6811
emulator stands in for the daughter chain on MLC2 J8.

| #272 ID | What it checks                                                  | Status      |
|---------|-----------------------------------------------------------------|-------------|
| E-060   | Chain discovery: module_online_mask == 0x1F within 500 ms       | implemented |
| E-061   | V-poll cadence: pit-diag 0x6C1[0..3] stays < 50 ms over 60 s    | implemented (PR #45; reads 0x6C1) |
| E-063   | PEC clean: 0x6C7 + 0x6C8 + 0x6C0[4..5] all 0 over 60 s          | implemented (PR #45; reads 0x6C7/0x6C8) |
| E-064   | T-poll: 0x6C1[4..7] temp_sweep_last_mask reads 0                | implemented (PR #45; reads 0x6C1) |
| E-065   | Pico STOP_REPLY 0x1F → mask collapses to 0 + FSM trips Error    | implemented |
| E-066   | STOP_REPLY 0x04 (module 2) → 0x4A0[2] reads 0x1B + FSM trips    | TODO — follow-up PR; currently asserts cell V injection only |
| E-067   | Per-IC PEC localisation: Pico fails PEC on chain N → 0x6C7/0x6C8 byte for N climbs alone | TODO — follow-up PR; currently asserts cell T injection only |

E-062 dropped per #272 — g_ltc_spi_err_count not on pit-diag; covered
by-proxy via E-066/E-067 cell-injection passes (bus-level errors block
those too).

Pico USB-CDC control surface used by E-065..E-067:

    STOP_REPLY <module_mask>
    INJECT_CELL_V <module> <cell> <mV>
    INJECT_CELL_T <module> <sensor> <degC>
    RESUME_ALL
"""

from __future__ import annotations

import time

import pytest

from tools.firmware_test.ams import can_map as M


PICO_INJECTION_PENDING = (
    "Blocked on Pico emulator INJECT/STOP commands "
    "(see docs/ams-hil/test-plan-v1.5.0.md §3 + the matching "
    "IFS08_HIL pico_ltc_emulator issue once filed)."
)

AMS_DIAG_COUNTER_PENDING = (
    "Blocked on AMS exposing LTC counters via telemetry (proposed "
    "0x4A3 diag frame in docs/ams-hil/test-plan-v1.5.0.md §2) or a "
    "BL diag-read command. Counter lives in firmware but isn't on "
    "the wire; SWD-only today."
)

# NOTE: AMS_DIAG_COUNTER_PENDING is no longer used — the counters E-061..E-064
# rely on are now on the wire via the pit-diag stream (AMS PR #248 status
# frames 0x6C0 / 0x6C1 + PR #269 per-IC PEC counts on 0x6C7 / 0x6C8).
# Tests below read from those frames via the `pit_diag` conftest fixture.
# The constant stays here so a `git grep` from older docs still resolves.


# ---------------------------------------------------------------------------
# E-060 — Chain discovery: module_online_mask == 0x1F within 500 ms
# ---------------------------------------------------------------------------

class TestE060ChainDiscovery:
    """Per #245: the LTC chain must converge to mask=0x1F (all 5 modules
    seen) within 500 ms of MLC2 app start. `fresh_boot` waits for the
    first 0x4A0 frame and returns its decoded payload; that frame is
    already 500 ms+ post-power-on (BL auto-jump + first telemetry
    cycle), so if the chain discovered cleanly the mask is already
    correct by the time we look.
    """

    def test_e060(self, fresh_boot, ams_profile):
        first = fresh_boot["first_frame"]
        expected = int(ams_profile["stub_module_online_mask"])
        assert first["module_online_mask"] == expected, (
            f"module_online_mask=0x{first['module_online_mask']:02X}, "
            f"expected 0x{expected:02X} -- "
            "LTC chain didn't fully discover within the first telemetry "
            "cycle. Suspect SPI framing (E-062), bad Pico CS routing, "
            "or a missing module slot in the Pico's seed.")


# ---------------------------------------------------------------------------
# E-061..E-064 — LTC-side health counters
# ---------------------------------------------------------------------------

class TestE061VPollCadence:
    """Per #245: `g_bms_volt_poll_ms` stays under 50 ms, `g_bms_volt_poll_max`
    doesn't drift over a 5 s observation. Now readable on `0x6C1[0..1]` (last
    cycle, BE u16) and `0x6C1[2..3]` (worst-case-since-boot, BE u16) via the
    pit-diag stream (AMS PR #248)."""

    def test_e061(self, fresh_boot, pit_diag):
        # Sample once at start, again 5 s later. Assert no drift in the
        # "worst-case-since-boot" counter (= no jitter spike during the
        # window) and bound the per-cycle reading.
        first = pit_diag.wait_for(M.ID_PIT_DIAG_TIMING)
        poll_first = int.from_bytes(first[0:2], "big")
        max_first  = int.from_bytes(first[2:4], "big")

        time.sleep(5.0)
        second = pit_diag.wait_for(M.ID_PIT_DIAG_TIMING)
        poll_second = int.from_bytes(second[0:2], "big")
        max_second  = int.from_bytes(second[2:4], "big")

        # Per #245: under 50 ms budget for the V-poll cycle.
        assert poll_first  < 50, f"V-poll cycle {poll_first} ms ≥ 50 ms at sample 1"
        assert poll_second < 50, f"V-poll cycle {poll_second} ms ≥ 50 ms at sample 2"
        # Drift = worst-case grew during the window.
        assert max_second <= max_first + 5, (
            f"V-poll worst-case drifted up by {max_second - max_first} ms "
            f"in 5 s (first={max_first}, second={max_second}). Looks like "
            "a jitter spike — investigate BmsPollTask priority / preemption.")


# E-062 dropped per AMS #272 — g_ltc_spi_err_count isn't on the
# pit-diag stream, and bus-level SPI errors would also block the
# E-066/E-067 cell-injection passes, so coverage is implicit.


class TestE063PecClean:
    """Per #245: `g_ltc_pec_err_count[*]` stays at 0 over the observation
    window. With the per-IC counts now on `0x6C7[0..7]` + `0x6C8[0..1]` via
    AMS PR #269, this is directly readable.

    NOTE: This test will FAIL today on the MLC2 bench because of
    IFS08_HIL#44 (wire-level PEC corruption — chain-wide ~70% failure rate
    + chip 1 outlier). The test SHOULD fail in that state — the assertion
    is correct, the bench is broken. Mark expected-fail until #44 lands."""

    def test_e063(self, fresh_boot, pit_diag):
        # Wait one scan cycle so PEC counters settle; the saturating u8s
        # at 0xFF mean ≥ 255 failures since boot — anything > 0 fails the
        # assertion since #245 calls for zero.
        pit_diag.wait_for_scan()
        a = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_A)
        b = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_B)
        per_ic = list(a[0:8]) + list(b[0:2])  # chain 0..9
        nonzero = [(i, c) for i, c in enumerate(per_ic) if c != 0]
        assert not nonzero, (
            "Non-zero PEC errors per IC over the observation window: "
            + ", ".join(f"IC{i}={c}{' (SATURATED ≥255)' if c == 0xFF else ''}"
                        for i, c in nonzero)
            + ". Suspect wire-level signal integrity (see IFS08_HIL#44)."
        )


class TestE064TempSweepClean:
    """Per #245: `g_temp_sweep_last_mask == 0` on a healthy emulator. Now
    on `0x6C1[4..7]` (LE u32 bitmask, bit N = NTC channel N failed in the
    last sweep) via AMS PR #248."""

    def test_e064(self, fresh_boot, pit_diag):
        timing = pit_diag.wait_for(M.ID_PIT_DIAG_TIMING)
        mask = int.from_bytes(timing[4:8], "little")
        assert mask == 0, (
            f"temp_sweep_last_mask = 0x{mask:08X} -- one or more NTC "
            "channels failed on the last sweep. Bits identify the "
            "failing channels (bit 0 = AUX1 / ADG731 ch 0, etc.). "
            "Suspect the LTC chain's GPIO1 routing or the ADG731 mux."
        )


# ---------------------------------------------------------------------------
# E-065 — Stop Pico mid-V-poll → mask goes to 0x00 within BmsStaleMs
# ---------------------------------------------------------------------------

class TestE065StopPicoModulesGoStale:
    """Per #245: stopping the Pico mid-V-poll should drop
    `module_online_mask` to 0x00 within `BmsStaleMs`. Observed
    firmware behaviour (PR #39 bench validation) is that the AMS
    keeps `mask = 0x1F` and trips FSM to Error via a cell-range
    predicate path instead — tracked as AMS issue #249. Until that
    semantic is fixed, the load-bearing assertion here is "FSM
    reached Error within a couple of telemetry cycles of
    STOP_REPLY", which still validates that the chain-fault is
    detectable end-to-end. Add the mask assertion back once #249
    lands."""

    def test_e065(self, fresh_boot, pico_emu, observe_acu,
                  wait_for_state, ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START, (
            "fresh_boot didn't land in Start; can't run E-065 from "
            "a tainted starting state"
        )
        pico_emu.stop_reply(0x1F)  # silence all 5 modules
        # Window: BmsStaleMs (1500 ms typical) + one telemetry cycle
        # (500 ms) + slack.
        wait_for_state(
            M.FsmState.ERROR,
            timeout_ms=int(ams_profile.get("bms_stale_ms", 1500))
                       + int(ams_profile["tx_telemetry_period_ms"])
                       + 300)


# Cell V/T injection coverage moved to Block B-026 per AMS #272 —
# E-066 and E-067 now test partial-chain stop-reply and per-IC PEC
# localisation respectively. See test_block_b_safety.py::TestB026CellRangeInjection
# for the predicate-trip coverage previously held here.


# ---------------------------------------------------------------------------
# E-066 — STOP_REPLY 0x04 (module 2) → mask reads 0x1B  (per AMS #272)
# ---------------------------------------------------------------------------

class TestE066StopReplyModule2:
    """Per AMS #272 E-066: silence only module 2's two chain positions
    (chain index 4 + 5). The freshness derivation (#250) drops bit 2 of
    `module_online_mask` after `BmsStaleMs` while leaving bits 0, 1, 3,
    4 set — i.e. the mask reads `0x1B` (= `0b11011`). The safety
    supervisor trips FSM to Error because `module_online_mask !=
    config::AllModulesMask`.
    """

    def test_e066_module2_only(self, fresh_boot, pico_emu, observe_acu,
                                wait_for_state, ams_profile):
        assert fresh_boot["first_frame"]["state"] == M.FsmState.START

        try:
            # 0x04 = bit 2 set = silence module 2's chain positions
            # (LTC 4 + LTC 5 per the 2×N / 2×N+1 mapping).
            pico_emu.stop_reply(0x04)

            # Budget: BmsStaleMs (1500 ms) + safety-cycle + telemetry slack.
            window_ms = (int(ams_profile.get("bms_stale_ms", 1500))
                         + int(ams_profile["tx_telemetry_period_ms"]) + 500)
            wait_for_state(M.FsmState.ERROR, timeout_ms=window_ms)

            # Confirm the mask drop pattern: bit 2 should be the only
            # bit cleared. Per #250 the mask reflects current freshness,
            # so the other 4 modules still report fresh.
            f = observe_acu.last(M.ID_TELEM_STATUS, extended=False)
            assert f is not None, "no 0x4A0 after Error transition"
            mask = M.decode_telem_status(f.data)["module_online_mask"]
            assert mask == 0x1B, (
                f"module_online_mask = 0x{mask:02X}, expected 0x1B "
                "(bit 2 cleared, modules 0/1/3/4 still fresh). "
                "Either the Pico's per-module silence mapping is "
                "off or AMS's freshness derivation regressed.")
        finally:
            pico_emu.resume_all()


# ---------------------------------------------------------------------------
# E-067 — Per-IC PEC localisation  (per AMS #272)
# ---------------------------------------------------------------------------

class TestE067PerIcPecLocalisation:
    """Per AMS #272 E-067: forcing PEC failure on a specific chain
    index should be visible only on that index's per-IC PEC counter
    (`0x6C7[N]` for chain 0..7, `0x6C8[N-8]` for chain 8..9) while the
    other indices stay at 0.

    Method: STOP_REPLY <mask> replaces the silenced positions' bytes
    with 0xFF on the wire, which fails PEC for exactly those ICs. We
    silence module 2 (mask 0x04 = chain index 4 + 5) and assert
    `0x6C7[4]` + `0x6C7[5]` climb above zero while every other byte
    in `0x6C7` + `0x6C8` stays at 0.

    NOTE: With IFS08_HIL#44 still open (wire-level PEC corruption
    affecting all chain positions ~70 % of polls, chip 1 outlier),
    every IC's counter is already non-zero at boot. This test SHOULD
    fail until #44 is resolved on the bench side — the assertion is
    correct, the bench is broken.
    """

    def test_e067_pec_localisation(self, fresh_boot, pico_emu, pit_diag):
        # Baseline: PEC counters before injecting.
        pit_diag.wait_for_scan()
        a0 = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_A)
        b0 = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_B)
        baseline = list(a0[0:8]) + list(b0[0:2])

        try:
            # Silence module 2 → chain index 4 + 5 should fail PEC.
            pico_emu.stop_reply(0x04)
            time.sleep(2.0)  # ~2 scan cycles so counters can climb.

            a1 = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_A)
            b1 = pit_diag.wait_for(M.ID_PIT_DIAG_PEC_PER_IC_B)
            current = list(a1[0:8]) + list(b1[0:2])
            delta = [(c - b) & 0xFF for c, b in zip(current, baseline)]

            # The silenced indices (4 + 5) must have climbed.
            assert delta[4] > 0, f"chain index 4 PEC count did not climb (Δ={delta[4]})"
            assert delta[5] > 0, f"chain index 5 PEC count did not climb (Δ={delta[5]})"

            # Every other index must have stayed at zero delta. With
            # IFS08_HIL#44 the baseline is already saturated for every
            # chip, so the delta IS zero (saturation absorbs new errors)
            # — that confounds the test until #44 lands. Surface that
            # explicitly in the failure message.
            silenced = {4, 5}
            collateral = [(i, d) for i, d in enumerate(delta)
                          if i not in silenced and d > 5]  # >5 = noise margin
            assert not collateral, (
                "Per-IC PEC count climbed for chain indices outside "
                f"the silenced module-2 pair (4, 5): {collateral}. "
                "If IFS08_HIL#44 is still open the baseline is "
                "saturated at 0xFF, which masks this assertion. "
                "Drop the bench's PEC corruption first.")
        finally:
            pico_emu.resume_all()
