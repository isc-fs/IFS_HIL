"""
Block BAL — operator balance-control override 0x103 (AMS #340 / #341).

Autonomous cell balancing runs **only in Charge**: a cell more than
`BalanceDeltaMv` (50 mV) above the pack minimum gets its DCC bit set, surfaced
on the pit-diag balance masks `0x6C2` (cells 0..63) / `0x6C3` (cells 64..94 +
cycle counts). The operator can override it with a magic-gated `0x103`:
'BALO' suppresses balancing, 'BALX' resumes auto; the override goes stale and
reverts to auto after `BalanceOverrideFreshMs` (5 s) of silence. The current
override state is mirrored on `0x6C0[2]` bit 2 (`balance_override`).

| #341 ID | Check                                                            |
|---------|------------------------------------------------------------------|
| B-01    | Charge + imbalance -> autonomous balancing (DCC set), flag 0      |
| B-02    | BALO -> DCC mask all-zero, flag 1                                 |
| B-03    | BALX -> balancing resumes, flag 0                                 |
| B-04    | stale (stop BALO) -> auto resumes after 5 s, flag 0               |
| B-05    | wrong magic (BALZ) is ignored                                     |
| B-06    | scope/safety: no effect outside Charge; never touches AMS_OK/Error|

The injected imbalance is on a module-0 cell, so its DCC bit lands in `0x6C2`
(`0x6C3` also carries cycle-count bytes, so the masks are read off `0x6C2`).
"""

from __future__ import annotations

import time
import pytest

from tools.firmware_test.ams import can_map as M
from tests.hil.ams.test_block_c_fsm import _require_inputs, _drive_to_charge


# Imbalance well above BalanceDeltaMv (50 mV) so the cell balances; nominal
# elsewhere. Module-0 cell-0 -> global cell 0 -> 0x6C2 bit 0.
_IMBALANCE_MODULE = 0
_IMBALANCE_CELL = 0
_IMBALANCE_MV = 3850       # +100 mV above the 3750 mV floor
_NOMINAL_MV = 3750


def _set_imbalance(pico_emu):
    pico_emu.set_all_cells(_NOMINAL_MV)
    pico_emu.inject_cell_v(module=_IMBALANCE_MODULE, cell=_IMBALANCE_CELL,
                           mV=_IMBALANCE_MV)


def _balancing_active(pit_diag) -> bool:
    """True iff a DCC bit is set on 0x6C2 (some module-0..3 cell is balancing).
    Reads a *fresh* pit-diag scan so it reflects the latest balance update."""
    pit_diag.wait_for_scan()
    return any(pit_diag.wait_for(M.ID_PIT_DIAG_BAL_MASK_A))


def _override_flag(pit_diag) -> bool:
    """0x6C0[2] bit 2 (balance_override). Reads the current scan's 0x6C0."""
    return bool(pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)[2]
                & M.BALANCE_OVERRIDE_BIT)


def _wait_balancing(pit_diag, want: bool, timeout_s: float = 12.0) -> bool:
    """Poll fresh pit-diag scans until 0x6C2 reaches the wanted active state."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _balancing_active(pit_diag) == want:
            return True
    return False


def _drive_to_charge_balancing(tsms, dash_chg, acu_heartbeat, charger_0x101,
                               pico_emu, wait_for_state, pit_diag, ams_profile):
    """Set the imbalance, drive to Charge, and wait for autonomous balancing
    to start. Shared B-01..B-05 setup."""
    _require_inputs(tsms, dash_chg)
    _set_imbalance(pico_emu)
    snap = _drive_to_charge(tsms, dash_chg, acu_heartbeat, charger_0x101,
                            wait_for_state, ams_profile)
    assert snap["state"] == M.FsmState.CHARGE
    assert _wait_balancing(pit_diag, want=True), (
        "autonomous balancing never started in Charge with a "
        f"{_IMBALANCE_MV - _NOMINAL_MV} mV imbalance (no DCC bit on 0x6C2). "
        "Balancing is Charge-only and gated on BalanceDeltaMv (50 mV).")


# ---------------------------------------------------------------------------
# B-01 — autonomous balancing baseline
# ---------------------------------------------------------------------------

class TestB01AutonomousBalancing:
    def test_b01_autonomous_balancing_in_charge(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, charger_0x101,
        pico_emu, wait_for_state, pit_diag, ams_profile):
        _drive_to_charge_balancing(tsms, dash_chg, acu_heartbeat, charger_0x101,
                                   pico_emu, wait_for_state, pit_diag, ams_profile)
        # No override in effect -> flag clear.
        assert not _override_flag(pit_diag), (
            "0x6C0[2] balance_override bit set with no 0x103 sent.")


# ---------------------------------------------------------------------------
# B-02 — BALO suppresses
# ---------------------------------------------------------------------------

class TestB02BaloSuppresses:
    def test_b02_balo_suppresses(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, charger_0x101,
        pico_emu, balance_override, wait_for_state, pit_diag, ams_profile):
        _drive_to_charge_balancing(tsms, dash_chg, acu_heartbeat, charger_0x101,
                                   pico_emu, wait_for_state, pit_diag, ams_profile)
        balance_override["send"]("BALO")
        assert _wait_balancing(pit_diag, want=False), (
            "BALO (0x103) did not suppress balancing: DCC bits still set on "
            "0x6C2 after the override.")
        assert _override_flag(pit_diag), (
            "0x6C0[2] balance_override bit not set while BALO is fresh.")


# ---------------------------------------------------------------------------
# B-03 — BALX resumes auto
# ---------------------------------------------------------------------------

class TestB03BalxResumes:
    def test_b03_balx_resumes(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, charger_0x101,
        pico_emu, balance_override, wait_for_state, pit_diag, ams_profile):
        _drive_to_charge_balancing(tsms, dash_chg, acu_heartbeat, charger_0x101,
                                   pico_emu, wait_for_state, pit_diag, ams_profile)
        balance_override["send"]("BALO")
        assert _wait_balancing(pit_diag, want=False), "BALO didn't suppress"
        # Resume.
        balance_override["send"]("BALX")
        assert _wait_balancing(pit_diag, want=True), (
            "BALX (0x103) did not resume balancing: DCC bit never re-appeared "
            "on 0x6C2.")
        assert not _override_flag(pit_diag), (
            "0x6C0[2] balance_override bit still set after BALX resumed auto.")


# ---------------------------------------------------------------------------
# B-04 — stale override reverts to auto
# ---------------------------------------------------------------------------

class TestB04StaleRevertsToAuto:
    def test_b04_stale_reverts_to_auto(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, charger_0x101,
        pico_emu, balance_override, wait_for_state, pit_diag, ams_profile):
        _drive_to_charge_balancing(tsms, dash_chg, acu_heartbeat, charger_0x101,
                                   pico_emu, wait_for_state, pit_diag, ams_profile)
        balance_override["send"]("BALO")
        assert _wait_balancing(pit_diag, want=False), "BALO didn't suppress"
        # STOP sending -> override goes stale after BalanceOverrideFreshMs.
        balance_override["stop"]()
        fresh_s = M.BALANCE_OVERRIDE_FRESH_MS / 1000.0
        assert _wait_balancing(pit_diag, want=True, timeout_s=fresh_s + 8.0), (
            f"balancing did not auto-resume within {fresh_s:.0f}s + slack of "
            "the last BALO -- the stale-revert (BalanceOverrideFreshMs) failed.")
        assert not _override_flag(pit_diag), (
            "0x6C0[2] balance_override bit still set after the override went "
            "stale.")


# ---------------------------------------------------------------------------
# B-05 — wrong magic is ignored
# ---------------------------------------------------------------------------

class TestB05MagicGate:
    def test_b05_wrong_magic_ignored(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, charger_0x101,
        pico_emu, balance_override, wait_for_state, pit_diag, ams_profile):
        _drive_to_charge_balancing(tsms, dash_chg, acu_heartbeat, charger_0x101,
                                   pico_emu, wait_for_state, pit_diag, ams_profile)
        # A 0x103 with a wrong payload ("BALZ") must be ignored.
        balance_override["send"]("BALZ")
        # Give it several scans -- balancing must stay ON, flag must stay clear.
        time.sleep(2.0)
        assert _balancing_active(pit_diag), (
            "a wrong-magic 0x103 (BALZ) suppressed balancing -- the magic gate "
            "is not rejecting unknown payloads.")
        assert not _override_flag(pit_diag), (
            "0x6C0[2] balance_override bit set by a wrong-magic 0x103 (BALZ).")


# ---------------------------------------------------------------------------
# B-06 — scope / safety: no effect outside Charge, never touches AMS_OK/Error
# ---------------------------------------------------------------------------

class TestB06ScopeAndSafety:
    """BALO outside Charge changes nothing (balancing is Charge-only) and must
    never affect the AIRs, AMS_OK, or Error latching. We drive to Run (Car) --
    the energised non-charge state -- send BALO, and confirm the FSM stays in
    Run with AMS_OK HIGH and no fault, and no balancing starts."""

    def test_b06_balo_no_effect_outside_charge(
        self, fresh_boot, tsms, dash_chg, acu_heartbeat, pico_emu,
        balance_override, wait_for_state, observe_acu, pit_diag, ams_profile):
        _require_inputs(tsms, dash_chg)
        from tests.hil.ams.test_block_c_fsm import _drive_to_run
        _set_imbalance(pico_emu)
        _drive_to_run(tsms, dash_chg, acu_heartbeat, wait_for_state, ams_profile)

        # Balancing is Charge-only: even imbalanced, Run has no DCC bits.
        assert not _balancing_active(pit_diag), (
            "balancing ran in Run -- it must be Charge-only.")

        # Send BALO in Run: must change nothing safety-relevant.
        balance_override["send"]("BALO")
        time.sleep(2.0)
        fsm = pit_diag.wait_for(M.ID_PIT_DIAG_FSM_STATUS)
        assert fsm[0] == M.FsmState.RUN, (
            f"FSM left Run to {M.FsmState.name(fsm[0])} after a BALO 0x103 -- "
            "the balance override must never affect the FSM / AIRs.")
        assert fsm[3] == 1, (
            "AMS_OK dropped after a BALO 0x103 in Run -- the override must "
            "never touch AMS_OK.")
        assert fsm[6] == 0, (
            f"fault_reason = {fsm[6]} after a BALO 0x103 in Run -- the override "
            "must never latch Error.")
        assert not _balancing_active(pit_diag), (
            "BALO started/affected balancing outside Charge.")
