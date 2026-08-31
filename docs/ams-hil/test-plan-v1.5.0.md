# AMS HIL test plan — v1.5.0 acceptance

Implementation tracker for [`isc-fs/IFS08-CE-AMS#245`](https://github.com/isc-fs/IFS08-CE-AMS/issues/245)
("Full HIL acceptance — AMS v1.5.0, post-Pico-emulator, post-stub").
Supersedes the v1.4.0 scope from `#193`.

This doc is the audit-trail and roadmap for converting #245's prose
into runnable pytest, plus the KPI infrastructure that quantifies
"how many hours of testing this methodology actually produces".

---

## 1. Block-by-block mapping

Symbols:
- ✅ implemented and runnable on the current bench
- 🟡 scaffolded with `@pytest.mark.skip(reason=...)`; blocked on an
  external prerequisite that's named in the reason
- ❌ not started

### Block A — boot + telemetry baseline

`tests/hil/ams/test_block_a_boot.py`

| #245 ID | Status | Implementation notes |
|---|---|---|
| A-001 | ✅ | Cold power → BL responsive — already wired (real ADC3 ch1/2/3 measurement, lands post-#16). |
| A-002 | ✅ | `can-flasher discover --node-id 0x01`. |
| A-003 | ✅ | App flashes + auto-jumps; first telemetry < 5 s. |
| A-004 | ✅ | App reaches Start within `SafetyBootGraceMs` (2 s). |
| A-005 | ✅ | First `0x4A0` decodes as Start with real LTC values (mask=0x1F, cell ≈ 3750 mV). |
| A-006 | ✅ | `0x4A1` pack_voltage_mV ≈ 356 250. |
| A-007 | ✅ | `0x4A2` temps + heartbeat sane (≈ 25 °C). |
| A-008 | ✅ | Telemetry cadence 500 ms ± 20 ms over 60 s. |
| **A-009** | ✅ | NEW — `firmware_info.reserved[0] == 0x01` (static binary check). |
| **A-010** | ✅ | NEW — `0x4A2[5]` cockpit byte in Start = `0x80` (valid + both LOW + Undecided). |
| **A-011** | ✅ | NEW — ECU TX matrix DLCs match contract (post-#238). |
| **A-012** | ✅ | NEW — FDCAN1 drops extended-ID frames at HW filter (post-#236). |

### Block B — safety predicates

`tests/hil/ams/test_block_b_safety.py`

| #245 ID | Status | Implementation notes |
|---|---|---|
| B-020 | 🟡 | Boot grace suppresses staleness — needs a "kill heartbeat at boot, observe Start for `SafetyBootGraceMs`" test. |
| B-021 | ✅ | VCU 0x100 stale > `VcuStaleMs` → Error — unblocked by #243. (Current test_b017 is this; rename in renumber PR.) |
| B-022 | 🟡 | BMS module stale → Error — needs a Pico-emulator `STOP_REPLY` command (per #239 reference). |
| B-023 | 🟡 | Current sensor stale → Error — flight build only (HIL builds relax this check). |
| B-024 | 🟡 | Current overlimit → Error — wired DAC4 ch0 is in `ams_profile.yaml`; needs the test to ramp the DAC past `CurrentMaxMa`. |
| B-025 | 🟡 | `sensor_fault` from ADC failure → Error — needs SWD-induced ADC fault. |
| **B-026** | 🟡 | NEW — cell UV/OV/OT/UT → Error via Pico injection. Blocked on Pico emulator `INJECT_CELL_V` / `INJECT_CELL_T` commands. |
| B-027 | 🟡 | `force_error_set` legacy hook — deferred, no consumer. |

### Block C — FSM transitions

`tests/hil/ams/test_block_c_fsm.py`

| #245 ID | Status | Implementation notes |
|---|---|---|
| C-030 | ✅ | Start stays with only TSMS — already test_c020_tsms_only. |
| C-031 | ✅ | Start stays with only DASH_CHG — already test_c020_dash_chg_only. |
| C-032 | ✅ | Start → Precharge on both + VCU fresh → Car mode — already test_c021. |
| C-033 | 🟡 | Precharge → Transition when DC bus ≥ 95 % pack — currently failing on timing race (test_c022). |
| ~~C-034~~ | — | REMOVED per #244 (no more `PrechargeMaxMs`). |
| ~~C-035~~ | — | REMOVED per #244 (no more `TransitionHoldMs`). |
| C-036 | ❌ | Transition voltage drop → Error. |
| C-037 | 🟡 | Charger mode: VCU stale + both inputs → Charge — currently failing (test_c026). |
| C-038 | 🟡 | Run → Error on TSMS drop — currently failing (test_c025_tsms_drop). |
| C-039 | ❌ | Run → Error on DASH_CHG drop. |
| C-040 | ❌ | Charge → Error on either drop. |
| C-041 | ❌ | `mode_locked` retained mid-Run when VCU killed. |
| C-042 | ❌ | `0x4A2[5]` cockpit byte across all 6 states. |
| C-043 | ✅ | Error sticky ≥ 5 s after heartbeat resumes — already test_c027. |
| C-044 | 🟡 | Error survives reset — flight build only. |
| **C-045** | ✅ | NEW — Start stays put with no fixtures driving PF9/PF10 (PR #230 pull-down contract). |

### Block D — bootloader integration

`tests/hil/ams/test_block_d_bootloader.py`

| #245 ID | Status | Implementation notes |
|---|---|---|
| D-050 | ✅ | Cold-cycle auto-jump (renumber from D-044). |
| D-051 | ✅ | Boot-trigger 0x002 reboots to BL — unblocked by #243 (renumber from D-041). |
| **D-051b** | ✅ | NEW — Trigger reboots from Error state too (safety property; renumber from D-041b). |

### Block E — LTC chain integrity (NEW BLOCK)

`tests/hil/ams/test_block_e_ltc.py`

| #245 ID | Status | Implementation notes |
|---|---|---|
| E-060 | ✅ | Chain discovery: mask=0x1F within 500 ms — observable via `0x4A0[2]`. |
| E-061 | 🟡 | V-poll cadence `g_bms_volt_poll_ms` < 50 ms — counter not on wire. Needs telemetry expose (see "Counters needed" below) or SWD. |
| E-062 | 🟡 | `g_ltc_spi_err_count == 0` over 60 s — same telemetry-expose dependency. |
| E-063 | 🟡 | `g_ltc_pec_err_count[*] == 0` over 60 s — same. |
| E-064 | 🟡 | `g_temp_sweep_last_mask == 0` — same. |
| E-065 | 🟡 | Stop Pico → `module_online_mask == 0x00` within `BmsStaleMs` — needs Pico `STOP_REPLY` command. |
| E-066 | 🟡 | Out-of-range cell V → predicate trips — needs Pico `INJECT_CELL_V`. |
| E-067 | 🟡 | Out-of-range cell T → predicate trips — needs Pico `INJECT_CELL_T`. |

### Block F — flash endurance (NEW BLOCK, all soak)

`tests/hil/ams/test_block_f_flash_endurance.py`

| #245 ID | Cycles | Status | Implementation notes |
|---|---|---|---|
| F-070 | 100 | 🟡 | Cold soak. Auto-runnable; defer to a `-m soak` mark so the default suite stays fast. |
| F-071 | 100 | 🟡 | CAN-trigger soak. Uses `tools/flash_ams_via_trigger.py`. |
| F-072 | 100 | 🟡 | Cross-trigger mix (cold + CAN alternating). |
| F-073 | 100 | 🟡 | CRC integrity per cycle — needs BL `READ_VERIFY` op or SWD. |
| F-074 | 20 | 🟡 | Bus-busy flash (heartbeat + noise during flash). |
| F-075 | 10 | 🟡 | Mixed-version round-trip — needs v1.4.0-eol image fixture. |
| F-076 | 5×2 | 🟡 | Stale-latch flash (with/without HIL_CLEAR_ERROR_LATCH). |
| F-077 | 10 | 🟡 | Interrupted-flash recovery — needs programmable PSU or relay-gated VBUS yank. |
| F-078 | 5×5 | 🟡 | Power-off duration sweep ({1, 5, 30, 60, 300} s). |
| F-079 | 1000 | 🟡 | DISCOVER latency long-soak. |
| F-080 | 20 | 🟡 | Trigger-from-Error (HIL_CLEAR=0). Needs flight build variant. |
| F-081 | 60 s | 🟡 | Bench-noise immunity (200 random standard-ID frames/s + valid trigger). |

### Other blocks (out of #245 scope but kept)

| Old block | Old ID range | Action |
|---|---|---|
| Old Block E (soak/smoke) | E-050..E-053 | Renamed to **Block G** — see `tests/hil/ams/test_block_g_soak.py`. Kept for the legacy 30-min idle/run soaks that don't fit the v1.5.0 acceptance plan but are still useful regression coverage. |
| Old Block F (relays) | F-060..F-066 | Renamed to **Block H** — see `tests/hil/ams/test_block_h_relays.py`. Per-relay toggle tests; orthogonal to flash endurance. |

---

## 2. Counters / probes needed

#245 Block F asks per-cycle records of:
- BL DISCOVER round-trip latency — available from `can-flasher` stdout.
- App first-telemetry timestamp delta — available from `observe_acu`.
- `g_app_init_progress` — not on the wire today; either expose via a
  diag CAN frame or accept SWD-only.
- `0x4A0[0]` state at first sample — available from `observe_acu`.
- `g_ltc_pec_err_count[*]` post-boot — same exposure problem.
- `firmware_info.git_hash` — already covered by D-043 / A-009 path.

**Counters that would unblock Block E + Block F soak rows:**

| Counter | Source | Proposed exposure |
|---|---|---|
| `g_bms_volt_poll_ms` | `bms_poll_task.cpp` | new diag frame 0x4A3 byte 0..1 (LE u16 ms) |
| `g_bms_volt_poll_max` | same | 0x4A3 byte 2..3 |
| `g_ltc_spi_err_count` | `ltc6811_io.cpp` | 0x4A3 byte 4 |
| `g_ltc_pec_err_count[0..4]` | same, per-module | 0x4A3 byte 5 (bit-OR) + per-mod breakdown on demand |
| `g_temp_sweep_last_mask` | `bms_temp_task.cpp` | 0x4A3 byte 6 |
| `g_app_init_progress` | `app_init_task.cpp` | 0x4A3 byte 7 |

If the AMS team prefers no new telemetry IDs, an alternative is a
poll-on-demand "diag read" CAN command (0x002-adjacent) that the AMS
answers with the snapshot. Tracking decision for #245 follow-up.

---

## 3. Pico emulator commands needed

For B-022/B-026 + E-065/E-066/E-067 — the Pico needs to be able to:

| Command | Purpose | Used by |
|---|---|---|
| `STOP_REPLY <module_mask>` | Stop responding to LTC SPI polls on the given module slots | B-022, E-065 |
| `INJECT_CELL_V <module> <cell> <mV>` | Override one cell's reported voltage | B-026, E-066 |
| `INJECT_CELL_T <module> <sensor> <degC>` | Override one cell's reported temperature | B-026, E-067 |
| `RESUME_ALL` | Drop all overrides, resume nominal seed values | every test that exits "trip" state |

Pico-side tracking: `tools/pico_ltc_emulator/` — file an issue against
that subtree when the test PR lands.

---

## 4. KPI instrumentation

The user's ask: *"prepare a series of KPIs to measure the hours of
testing achieved by this methodology"*.

The KPI plugin (`tests/hil/ams/kpi_plugin.py`) hooks pytest's session
lifecycle and emits a JSON ledger per session. Aggregator script
(`scripts/hil_kpi_report.py`) rolls per-session ledgers into a
multi-session report.

The plugin is registered from the rootdir `conftest.py` — pytest only
honours `pytest_plugins` there — so it loads on every invocation, not
just AMS ones. `--no-kpi` turns it off.

**Per-session metrics:**

| Metric | What it measures | Why it matters |
|---|---|---|
| `session_wall_clock_s` | Total time from `pytest_sessionstart` to `pytest_sessionfinish`. | The raw "bench-hours" number. Includes setup/teardown overhead. |
| `tests_executed` | Count of tests that ran (passed + failed + errored, excluding skipped/deselected). | Throughput. |
| `tests_passed` / `tests_failed` / `tests_errored` / `tests_skipped` | Per-outcome breakdown. | Quality + coverage. |
| `total_test_time_s` | Sum of per-test wall-clock (the `duration` pytest already reports). | "Active testing" time excluding fixture overhead between tests. |
| `bench_utilisation_pct` | `total_test_time_s / session_wall_clock_s × 100`. | How efficient the session was (overhead vs real work). |
| `power_cycle_count` | Number of MLC power-cycles invoked. | Wear estimator (relay K2, MLC2 carrier connector reseat budget). |
| `flash_cycle_count` | Number of `can-flasher flash` invocations. | BL/STM32 flash-write endurance estimator (10k writes per sector spec). |
| `bl_trigger_count` | Number of `0x002` trigger frames sent. | Validates the trigger path got real exercise. |
| `frames_observed_total` | Frame count across all `observe_acu` sniffers. | Raw bus-event coverage. |
| `block_F_cycles_completed` | Cycles in soak rows that actually completed (counts toward the #245 acceptance gate). | The "100 cycles for F-070" measurement. |

**Cross-session aggregates (the "hours of testing" headline):**

| Metric | Why it matters |
|---|---|
| `cumulative_bench_hours` | Sum of `session_wall_clock_s` across all sessions in the ledger window — the headline number. |
| `cumulative_test_hours` | Sum of `total_test_time_s` — the "active testing" number that excludes idle bench time. |
| `cumulative_power_cycles` | Sum of `power_cycle_count` — for relay K_n and MLC connector wear tracking. |
| `cumulative_flash_cycles` | Sum of `flash_cycle_count` — for flash-sector endurance tracking (spec ~10k writes per sector). |
| `regressions_caught` | Count of tests that PASSED in a prior session and FAILED in a later one. | Detects the "we broke X" moments. |
| `flakes_observed` | Count of tests that have both passes and failures across sessions (without a known firmware change). | Quality of the suite itself. |
| `equivalent_operator_hours` | Heuristic estimate (see below). | "What would a person have spent doing this manually?" |
| `mean_time_to_BL_discover` / `p99_time_to_BL_discover` | Telemetry on the flash pipeline. | Detects BL regression early. |

**Equivalent operator hours (heuristic):**

For each cycle a human operator would:
- Power-cycle: ~30 s (toggle PSU, wait for boot)
- Flash: ~60 s (open tool, click flash, wait for verify)
- Cold-boot observation: ~15 s (look at telemetry, decide pass/fail)
- BL trigger test: ~45 s (send frame, observe behavior, recover state)
- Per FSM transition assertion: ~30 s

`equivalent_operator_hours = (power_cycle_count × 30 + flash_cycle_count × 60 + bl_trigger_count × 45 + fsm_transitions_observed × 30) / 3600`

This is deliberately optimistic for the human — assumes they don't make mistakes, don't need to re-flash, don't get distracted. The real ratio is typically 2-5× more.

**Output formats:**
- `.kpi/<session_id>.json` per session (raw)
- `.kpi/summary.md` aggregated, human-readable
- `.kpi/trend.csv` time-series for dashboards

---

## 5. PR plan

| Branch | Scope | Status |
|---|---|---|
| `feat/ams-hil-v1.5.0-acceptance` (this PR) | Plan doc + KPI plugin + new Block A tests + Block E/F scaffolds + Block D renumber + block rename of old E (soak) and F (relays) | in-flight |
| `test/ams-hil-renumber-blocks-bc` (follow-up) | Renumber B-010..B-017 → B-020..B-027 and C-020..C-028 → C-030..C-045, delete obsolete C-034/C-035 | not started |
| `feat/ams-hil-block-e-ltc-counters-impl` (follow-up) | Implement E-061..E-064 once AMS exposes the diag counters | blocked on AMS-side decision |
| `feat/ams-hil-block-f-soak-impl` (follow-up) | Wire up the actual soak loops once the trigger-flash helper is in the operator's hands | blocked on operator opting in |
| `feat/pico-emulator-injection-commands` (follow-up, IFS_HIL) | Pico-side STOP_REPLY / INJECT_CELL_V / INJECT_CELL_T per §3 | blocked on Pico emulator branch owner |
