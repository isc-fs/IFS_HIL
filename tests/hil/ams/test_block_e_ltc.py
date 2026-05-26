"""
Block E — LTC chain integrity.

Per `isc-fs/IFS08-CE-AMS#245`. The real LTC path runs in every build
post-#207 (HIL_STUB removed); on the bench the Pi Pico LTC6820/LTC6811
emulator stands in for the daughter chain. All tests assume the Pico
is wired on MLC2 J8 per `tools/pico_ltc_emulator/`.

| Test  | What it checks                                                  | Status      |
|-------|-----------------------------------------------------------------|-------------|
| E-060 | Chain discovery: module_online_mask == 0x1F within 500 ms       | implemented |
| E-061 | V-poll cadence: g_bms_volt_poll_ms stays < 50 ms                | scaffolded — diag counter not on wire |
| E-062 | SPI framing clean: g_ltc_spi_err_count == 0 over 60 s           | scaffolded — diag counter not on wire |
| E-063 | PEC clean: g_ltc_pec_err_count[*] == 0 over 60 s                | scaffolded — diag counter not on wire |
| E-064 | T-poll: g_temp_sweep_last_mask == 0                             | scaffolded — diag counter not on wire |
| E-065 | Stop Pico mid-V-poll → mask=0x00 within BmsStaleMs              | scaffolded — needs Pico STOP_REPLY cmd |
| E-066 | Inject out-of-range cell V → cell-range predicate trips         | scaffolded — needs Pico INJECT_CELL_V |
| E-067 | Inject out-of-range cell T → cell-T predicate trips             | scaffolded — needs Pico INJECT_CELL_T |

The four diag-counter rows (E-061..E-064) need either a new diag CAN
frame from the AMS firmware (proposed: `0x4A3` packed with the LTC
counter snapshot — see `docs/ams-hil/test-plan-v1.5.0.md` §2) or a
poll-on-demand "diag read" command. They're scaffolded with a clear
skip-reason so they show up in the scoreboard as "blocked on AMS
exposure decision" rather than silently absent.

The injection rows (E-065..E-067) need three new commands on the
Pico's USB-CDC control surface:

    STOP_REPLY <module_mask>
    INJECT_CELL_V <module> <cell> <mV>
    INJECT_CELL_T <module> <sensor> <degC>
    RESUME_ALL

See test-plan-v1.5.0.md §3. Once those land, drop the skip and
implement the test bodies — the scaffolds below have the assertion
shape spelled out so the implementation is a fill-in-the-blanks job.
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
    @pytest.mark.skip(reason=AMS_DIAG_COUNTER_PENDING)
    def test_e061(self):
        # Sketch once the counter is on the wire:
        #   wait 60 s, sample 0x4A3 at end, assert
        #   g_bms_volt_poll_ms_p99 < 50 and g_bms_volt_poll_max
        #   doesn't drift across the window.
        pass


class TestE062SpiFramingClean:
    @pytest.mark.skip(reason=AMS_DIAG_COUNTER_PENDING)
    def test_e062(self):
        # Sketch: sample 0x4A3 over 60 s, assert g_ltc_spi_err_count
        # delta == 0 (no error increments across the window).
        pass


class TestE063PecClean:
    @pytest.mark.skip(reason=AMS_DIAG_COUNTER_PENDING)
    def test_e063(self):
        # Sketch: per-module PEC counter sum delta == 0 over 60 s.
        pass


class TestE064TempSweepClean:
    @pytest.mark.skip(reason=AMS_DIAG_COUNTER_PENDING)
    def test_e064(self):
        # Sketch: g_temp_sweep_last_mask reads 0 (no channel failed
        # on the last sweep) at any point after fresh_boot.
        pass


# ---------------------------------------------------------------------------
# E-065 — Stop Pico mid-V-poll → mask goes to 0x00 within BmsStaleMs
# ---------------------------------------------------------------------------

class TestE065StopPicoModulesGoStale:
    @pytest.mark.skip(reason=PICO_INJECTION_PENDING)
    def test_e065(self, fresh_boot, observe_acu, ams_profile):
        # Sketch once Pico has STOP_REPLY:
        #   pico_client.stop_reply(mask=0x1F)
        #   deadline = monotonic() + (BmsStaleMs + 1 cycle + slack) / 1000
        #   while monotonic() < deadline:
        #       f = observe_acu.last(M.ID_TELEM_STATUS)
        #       if f and decode_telem_status(f.data)["module_online_mask"] == 0x00:
        #           return
        #       sleep(0.05)
        #   raise AssertionError("mask never went to 0x00")
        pass


# ---------------------------------------------------------------------------
# E-066 — Inject out-of-range cell V → cell-range predicate trips
# ---------------------------------------------------------------------------

class TestE066InjectCellOverVoltage:
    @pytest.mark.skip(reason=PICO_INJECTION_PENDING)
    def test_e066_over(self, fresh_boot, observe_acu, wait_for_state,
                       ams_profile):
        # Sketch:
        #   pico_client.inject_cell_v(module=0, cell=5, mV=4400)
        #   wait_for_state(M.FsmState.ERROR, timeout_ms=...)
        #   verify last 0x4A0 max_cell_mV >= 4400 (firmware
        #   actually saw the injection)
        pass

    @pytest.mark.skip(reason=PICO_INJECTION_PENDING)
    def test_e066_under(self, fresh_boot, observe_acu, wait_for_state,
                        ams_profile):
        # Same shape, but inject mV=2500 and assert min_cell_mV <= 2500.
        pass


# ---------------------------------------------------------------------------
# E-067 — Inject out-of-range cell T → cell-T predicate trips
# ---------------------------------------------------------------------------

class TestE067InjectCellOverTemperature:
    @pytest.mark.skip(reason=PICO_INJECTION_PENDING)
    def test_e067_over(self):
        # Sketch:
        #   pico_client.inject_cell_t(module=0, sensor=2, degC=75)
        #   wait_for_state(ERROR)
        #   assert max_tempC seen >= 60 (over-temp predicate threshold)
        pass

    @pytest.mark.skip(reason=PICO_INJECTION_PENDING)
    def test_e067_under(self):
        # Same shape, inject degC=-20 and assert min_tempC <= -10.
        pass
