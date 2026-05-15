"""
Block E — Bootloader integration tests.

The AMS app v1.2.0-ltc6811 (#73) listens for a boot-trigger frame on
FDCAN1 (`can0` in bench parlance): standard ID `0x002`, DLC 4, payload
`B0 07 AD 11`. When matched, `AcuCanTask` calls `Bootloader::request_reboot`
which writes `RTC_BKP_DR0` magic + `NVIC_SystemReset`. The chip reboots
through the BL.

On this bench the BL's auto-jump back to the app is faster than any
post-reset `discover` can catch, so we observe the reset via the
0x4A2 "AMS temps + diagnostics" heartbeat counter (byte 7), which is
incremented every 500 ms telemetry cycle and resets to 0 each app boot.
A *reset* is therefore a discontinuity where the counter jumps backward
or holds a low value after sustained higher values — concretely,
seeing a counter <= 5 within a couple of seconds of a stimulus that
was supposed to cause a reset.

Block E test IDs implemented here (per isc-fs/IFS08-CE-AMS#104):
  HIL-041  Boot-trigger round-trip (FDCAN1 0x002 # B007AD11)
  HIL-042  Wrong-bus trigger ignored
  HIL-043  Wrong-payload trigger ignored (6 sub-cases)
  HIL-044  Wrong-DLC trigger ignored

Not implemented here (require a scope or SWD):
  HIL-045  Pre-reboot relay-open timing
  HIL-046  BKP0R cleared by BL (one-shot) — partially testable
  HIL-047  Flood of malformed + one valid — composite, future
"""

from __future__ import annotations

import subprocess
import time

import pytest

from tools.firmware_test.ams import can_map as M


# ---------------------------------------------------------------------------
# Heartbeat-counter watch
# ---------------------------------------------------------------------------

def _read_heartbeat(observe_acu) -> int | None:
    """Latest heartbeat counter (byte 7 of 0x4A2) or None if no frame yet."""
    frame = observe_acu.last(M.ID_TELEM_TEMPS, extended=False)
    if frame is None:
        return None
    return M.decode_telem_temps(frame.data)["heartbeat"]


def _wait_for_heartbeat_advance(observe_acu, baseline: int,
                                period_ms: int, n_periods: int = 3,
                                timeout_s: float = 2.5) -> int | None:
    """Wait until the heartbeat counter advances by at least `n_periods`
    relative to `baseline`. Returns the new counter, or None on timeout.

    Counter wraps at 255, so we compute the diff modulo-256.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        hb = _read_heartbeat(observe_acu)
        if hb is not None:
            diff = (hb - baseline) % 256
            if diff >= n_periods:
                return hb
        time.sleep(period_ms / 1000.0 / 4)
    return None


def _heartbeat_reset_detected(observe_acu, baseline_counter: int,
                              window_s: float = 1.5) -> bool:
    """Watch for a heartbeat counter discontinuity that indicates a reset.

    A counter jumping from `baseline_counter` (presumably high) down to
    a low value (<= 5) is the reset signature: the chip rebooted, app
    re-init'd, TelemetryTask started its counter from 0 again.

    Returns True on reset detected, False if the counter kept advancing
    normally throughout the window.
    """
    deadline = time.time() + window_s
    last_seen = baseline_counter
    while time.time() < deadline:
        hb = _read_heartbeat(observe_acu)
        if hb is not None and hb != last_seen:
            # A counter that's both LOW (<= 5) AND much lower than baseline
            # indicates a fresh-app counter.
            backwards_jump = (last_seen - hb) % 256
            if hb <= 5 and backwards_jump > 5:
                return True
            last_seen = hb
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Test fixture — ensure app is alive and emitting telemetry first
# ---------------------------------------------------------------------------

@pytest.fixture
def app_running(ams_profile, mlc_powered, bms_emulator, observe_acu,
                acu_heartbeat):
    """Power-cycle MLC1, let BL auto-jump to app, return once telemetry
    is flowing. Subsequent Block E tests can probe trigger handling
    from a known-running state."""
    from broker.server import BrokerClient
    import os
    client = BrokerClient(os.environ.get("HIL_BROKER_SOCKET",
                                         "/run/hil-broker/broker.sock"))
    slot_pin = int(ams_profile["mlc_slot"]) - 1
    try:
        client.call("tca.write_pin", addr=0x20, port=0, pin=slot_pin, value=False)
        time.sleep(2.0)
        observe_acu.clear()
        client.call("tca.write_pin", addr=0x20, port=0, pin=slot_pin, value=True)
    finally:
        client.close()

    # Wait for the app to come up. We need a heartbeat counter that's
    # advanced past a few cycles so a reset-to-low is unambiguous.
    period_ms = int(ams_profile["tx_telemetry_period_ms"])
    deadline = time.time() + 5.0 + (period_ms / 1000.0) * 6
    initial = None
    while time.time() < deadline:
        hb = _read_heartbeat(observe_acu)
        if hb is not None:
            if initial is None:
                initial = hb
            elif (hb - initial) % 256 >= 6:
                # Counter has advanced ≥ 6 ticks — app is steady, run can begin.
                return {"baseline_counter": hb}
        time.sleep(0.05)
    pytest.skip("App didn't come up with a steady heartbeat — Block A may "
                "have regressed; investigate before treating this as a pass.")


# ---------------------------------------------------------------------------
# HIL-041 — boot-trigger round-trip
# ---------------------------------------------------------------------------

class TestBootTrigger:

    @pytest.mark.xfail(
        reason=(
            "FDCAN1 standard-ID RX is broken on the current AMS build — "
            "MX_FDCAN1_Init sets StdFiltersNbr=0 and on H7 the "
            "accept-unmatched-standards path doesn't fall through. Both "
            "the boot trigger (0x002) and 0x600 (start button) frames "
            "are dropped silently while extended IDs come through. "
            "Tracked at isc-fs/IFS08-CE-AMS#104. Remove this xfail "
            "once StdFiltersNbr is bumped to ≥ 1 with a wildcard "
            "accept filter."
        ),
        strict=True,
    )
    def test_hil041_trigger_causes_reset(self, app_running, observe_acu,
                                         ams_profile):
        baseline = app_running["baseline_counter"]
        subprocess.run(["cansend", ams_profile["bus_acu"], "002#B007AD11"],
                       check=True, timeout=2)
        assert _heartbeat_reset_detected(observe_acu, baseline), (
            "Heartbeat counter didn't reset after sending the boot-trigger "
            "frame. Either AcuCanTask didn't process the frame, "
            "Bootloader::matches_trigger returned false, or "
            "NVIC_SystemReset didn't fire."
        )


# ---------------------------------------------------------------------------
# HIL-042 — wrong-bus trigger ignored
# ---------------------------------------------------------------------------

class TestWrongBus:

    def test_hil042_trigger_on_bms_bus_ignored(self, app_running, observe_acu,
                                               ams_profile):
        """Send the trigger payload on the BMS bus (can2). The AMS app
        only listens for it on FDCAN1 (the ACU bus), so this should
        NOT reset the chip."""
        baseline = app_running["baseline_counter"]
        subprocess.run(["cansend", ams_profile["bus_bms_bl"], "002#B007AD11"],
                       check=True, timeout=2)
        assert not _heartbeat_reset_detected(observe_acu, baseline), (
            "Heartbeat counter reset on a BMS-bus trigger. The AMS app "
            "shouldn't process boot-trigger frames on FDCAN2 — only FDCAN1."
        )


# ---------------------------------------------------------------------------
# HIL-043 — wrong-payload trigger ignored (6 sub-cases)
# ---------------------------------------------------------------------------

# Each is "almost the magic", one byte off. Boot-trigger MUST be exact.
WRONG_PAYLOADS = [
    "002#B007AD12",  # last byte wrong
    "002#B007AC11",  # third byte wrong
    "002#B107AD11",  # first byte wrong
    "002#00000000",  # all zeros
    "002#FFFFFFFF",  # all ones
    "002#11AD07B0",  # endianness-swapped magic
]


class TestWrongPayload:

    @pytest.mark.parametrize("frame", WRONG_PAYLOADS)
    def test_hil043_wrong_payload_ignored(self, app_running, observe_acu,
                                          ams_profile, frame):
        baseline = app_running["baseline_counter"]
        subprocess.run(["cansend", ams_profile["bus_acu"], frame],
                       check=True, timeout=2)
        assert not _heartbeat_reset_detected(observe_acu, baseline), (
            f"Heartbeat counter reset on payload `{frame}`. The boot-trigger "
            "match must be exact on all 4 bytes."
        )


# ---------------------------------------------------------------------------
# HIL-044 — wrong-DLC trigger ignored
# ---------------------------------------------------------------------------

# cansend uses the count of bytes after '#' as DLC, so these are
# effectively DLC=0, 2, 3, 5, 7 — anything ≠ 4.
WRONG_DLCS = [
    "002#",              # DLC 0 (data frame remote-style; cansend rejects? — keep)
    "002#B007",          # DLC 2
    "002#B007AD",        # DLC 3
    "002#B007AD1100",    # DLC 5
    "002#B007AD11000000",  # DLC 7
]


class TestWrongDLC:

    @pytest.mark.parametrize("frame", WRONG_DLCS)
    def test_hil044_wrong_dlc_ignored(self, app_running, observe_acu,
                                      ams_profile, frame):
        baseline = app_running["baseline_counter"]
        r = subprocess.run(["cansend", ams_profile["bus_acu"], frame],
                           capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            # cansend rejected the frame — that's fine, the bus never saw
            # it. Move on without asserting reset behaviour for this DLC.
            pytest.skip(f"cansend rejected `{frame}`: {r.stderr.strip()}")
        assert not _heartbeat_reset_detected(observe_acu, baseline), (
            f"Heartbeat counter reset on `{frame}`. Boot-trigger DLC must "
            "be exactly 4."
        )
