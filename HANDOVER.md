# IFS_HIL — maintainer handover

You are inheriting the **IFS_HIL** Hardware-in-the-Loop testbench: a
Raspberry Pi 4 on a custom backplane PCB that builds Formula Student STM32
firmware, flashes it over CAN, and runs a `pytest` regression suite against
real hardware — triggered by a label on a firmware PR. This document is the
"start here" for taking it over: what exists, how to stand up another bench,
how CI works, and what is fragile.

**Read it once, top to bottom.** Then keep [`CLAUDE.md`](CLAUDE.md) open as
the day-to-day operator cheat-sheet, and follow the pointers for depth.

- Author / current owner: **Raúl Morán** (ISC Racing Team), GitHub
  `raulmoranguerra`.
- Repo: [`isc-fs/IFS_HIL`](https://github.com/isc-fs/IFS_HIL). Trunk is
  **`dev`**, not `main` — see [§7](#7-known-drift--cleanup-backlog).
- Firmware repos it serves: `isc-fs/IFS08-CE-ECU` and `isc-fs/IFS08-CE-AMS`
  — the two with build recipes in [`configs/firmware/`](configs/firmware/).
  The micro-DV repo, `isc-fs/IFS08-DV-uDV`, has the PR trigger and the
  credential wired, but no recipe and no bench declaring `dut-udv` yet.
- Snapshot date of this document: **2026-09-26** (`dev` @ `5668b64`).

---

## 1. The bench in five minutes

A Raspberry Pi 4 plugs into the **BACKPLANE_HIL** PCB, which carries four
**MLC carrier slots** (STM32H733ZG daughter-boards — the ECUs under test),
3 CAN channels, 4 INA226 current monitors, 4 DAC80504s, 3 MCP3208 ADCs,
3 TCA9555 I/O expanders, and an ATX PSU interface.

Four things do all the work:

| Piece | What it is | Where |
|---|---|---|
| **`hil-broker`** | Python daemon; the *only* opener of SPI / I²C / GPIO. Everything else talks to it over a Unix-socket JSON-RPC. | [`broker/`](broker/), [`docs/broker-api.md`](docs/broker-api.md) |
| **kernel `mcp251x`** (patched) | Drives the 3 MCP2515s as SocketCAN `can0/1/2`. | [`infra/kernel-module/`](infra/kernel-module/mcp251x-patched/) |
| **`can-flasher`** | External Rust binary (`isc-fs/MingoCAN` releases) that speaks the STM32 bootloader protocol over CAN. Bypasses the broker. | not in this repo |
| **CI flash chain** | GitHub Actions: build firmware in the cloud → flash on the bench's self-hosted runner → run pytest → comment the verdict on the firmware PR. | [`.github/workflows/hil-test.yml`](.github/workflows/hil-test.yml) |

The operator mental model and the hard invariants are in
[`CLAUDE.md`](CLAUDE.md); the component tour is
[`docs/architecture.md`](docs/architecture.md); the signal-level map is
[`docs/hardware-reference.md`](docs/hardware-reference.md), whose source of
truth is the code file [`tools/hw_config.py`](tools/hw_config.py).

**Three facts that cause most confusion, up front:**

1. **CAN netdev names are inverted vs the PCB silk.** Kernel `can2` = PCB
   CAN1 = where the carriers live. Carrier flashes target `can2`. Do not
   "fix" this — it is the kernel's probe order.
2. **Update the bench with rsync, not git.** `~/IFS_HIL` on the Pi is the
   tree the services run from. After the first install (a plain `git clone`
   of this public repo) it is updated from a developer machine with
   [`scripts/sync_to_pi.sh`](scripts/sync_to_pi.sh) — **never `--delete`** —
   so uncommitted work can be tested and no GitHub credentials live on the
   bench. The CI runner keeps its own separate checkout under
   `~/actions-runner/`.
3. **SPI is dead until the PSU is on.** The MISO buffer is enabled in
   hardware by ATX `PWR_OK`, so every SPI operation presupposes
   `psu.status()['pwr_ok']`.

---

## 2. Current state (2026-09-26)

- **One live bench: `bench-01`** (Raúl's; `isc@192.168.0.123` on the lab
  LAN, `isc@100.96.95.78` via Tailscale). Descriptor:
  [`configs/benches/bench-01.yaml`](configs/benches/bench-01.yaml). It
  declares `dut-ams`, `dut-ecu`, the cell / temperature / pack-current / SDC
  stimulus capabilities, and the temp-open / module-silent faults. It does
  **not** claim `fault-cell-open` or `radio-nrf24` (U23 unpopulated). Its
  runner is **online** with exactly those labels.
- **The chain has failed silently twice, in overlapping windows — and both
  times the bench looked healthy:**
  1. **Workflow dead for 23 days** (2026-09-02 → 2026-09-25). An invalid
     `${{ … }}` expression in `hil-test.yml` made every dispatch a *startup
     failure*: zero jobs, no check-run — so `gh pr checks` reads the PR as
     **green**. Fixed in PR #138.
  2. **Runner dead for about a week** (from 2026-09-18). GitHub sent a
     transient "registration deleted"; the runner exited cleanly, and the
     stock runner unit has no `Restart=`, so nothing brought it back. Every
     dispatch queued against an offline runner and timed out while the
     watchdog reported the hardware `healthy`. Fixed in PR #141 (a
     `Restart=always` drop-in, and the watchdog now reports `RUNNER DOWN`).

  **The lesson: on this bench, silence is not success.** Confirm a run
  actually *started* (it has jobs) and that a verdict came back — not
  merely that nothing is red.
- **The repaired chain has run end-to-end once since.** Dispatch run
  [`36193122166`](https://github.com/isc-fs/IFS_HIL/actions/runs/36193122166)
  (2026-09-25, AMS v3.0.2): `resolve` ✓, cloud `build` ✓ via the primary
  artifact path, and on the bench `flash_dut` isolated the other carrier,
  powered MLC2, sent the boot trigger, found exactly one bootloader and
  passed the identity gate — then the flash timed out, because the AMS
  bootloader had been re-provisioned to node `0x02` that same day while its
  profile still addressed `0x01`. PR #142 (2026-09-26) fixed the profile.
  **No run has happened since #142**, and the *label* path — the one that
  uses the cross-repo credentials — has not been exercised since the
  repair. That is the first thing to do
  ([§8](#8-first-tasks-for-the-next-maintainer)).
- **The bench self-heals.** `hil-bench-watchdog.timer` runs every 5 min and
  clears the two recurring hardware wedges (DAC, `mcp251x`) with nobody
  watching; it also flags a dead runner. See [§6](#6-operating-it).
- **All 12 SPI chip-selects are kernel-owned** since 2026-09-04 — the fix for
  the DAC-wedge race (#124).
- **Carrier node ids follow the scheme** ECU = `0x01`, AMS = `0x02`,
  uDV = `0x03`: bench-01's AMS bootloader was re-provisioned `0x01 → 0x02`
  over SWD on 2026-09-25 (#140 / PR #142). Ids live in each carrier's
  bootloader NVM and can drift, which is why
  [`tools/flash_dut.py`](tools/flash_dut.py) isolates by **slot** (powers
  only the target carrier) and gates on the bootloader's **product string**
  — never on the node id.

---

## 3. Repository map

```
IFS_HIL/
├── HANDOVER.md          ← you are here
├── CLAUDE.md            operator cheat-sheet · branch/commit policy · invariants
├── README.md            project overview
├── broker/              hil-broker daemon (server, rpc, bus, fake_bus)
├── dashboard/           Flask observability UI (:8080)
├── tools/               hw_config.py (SSOT) · bench.py (fleet CLI) · flash_dut.py
│                        · hil_client.py · drivers.  flash.py / mcp2515.py = LEGACY
├── tests/               tests/broker/ (off-bench, fake) · tests/hil/ (on-bench:
│                        top-level = bench self-tests; vcu/ + ams/ = DUT suites)
├── configs/
│   ├── benches/         capability descriptors (schema.json + bench-01.yaml)
│   ├── firmware/        build recipes (ecu.yaml, ams.yaml) — reviewed HERE
│   ├── suites.yaml      named test suites (smoke / dv / full / …)
│   └── ecu_*.yaml       LEGACY per-ECU configs (old chain) — for removal
├── infra/
│   ├── systemd/         hil-* units + actions.runner.restart.conf (runner drop-in)
│   ├── devicetree/      mcp2515-triple.dts — the 12-chip-select SPI overlay
│   ├── kernel-module/   patched mcp251x (5 patches) + build.sh
│   ├── sudoers.d/       narrow `ip link set canN` escalation for the broker
│   └── udev/            ST-Link + (legacy) USB-CAN rules
├── docker/              firmware-build image (ARM GCC 12.3) — legacy chain only
├── scripts/             bench_setup.sh (provisioning) · sync_to_pi.sh · …
└── .github/workflows/   TWO chains — see §5
```

`firmware/` is intentionally empty in the repo and absent on the bench: the
firmware lives in the per-ECU repos and CI checks it out at an exact SHA.

---

## 4. Standing up a new Pi / bench

**The automated path is [`scripts/bench_setup.sh`](scripts/bench_setup.sh)** —
a resumable, idempotent "freshly imaged Pi → fleet-registered bench" script.
The explain-every-step manual version is
[`docs/getting-started.md`](docs/getting-started.md); read it once so you can
debug the script when it stops.

```sh
# on the new Pi (Raspberry Pi OS Lite 64-bit, Bookworm+), after cloning IFS_HIL
scripts/bench_setup.sh --bench bench-02 --dry-run   # report only, change nothing
scripts/bench_setup.sh --bench bench-02             # base install
sudo reboot                                          # the script stops and asks
scripts/bench_setup.sh --bench bench-02             # continues after the reboot
```

In order, it: enables interfaces + groups → installs apt packages (incl.
`gcc-arm-none-eabi` + `cmake` for the artifact-fallback build) →
`pip install -e '.[bench]'` → compiles + installs the device-tree overlay and
edits `config.txt` → builds + installs the patched `mcp251x` → installs the
sudoers drop-in → enables the systemd units → **reboot gate** → runs
`bench doctor` (host matches the documented build) → installs `can-flasher` →
drafts the bench descriptor → registers the self-hosted runner, **enabled
and with the `Restart=always` drop-in** so it survives both a power cut and
the runner quitting.

### What the script does NOT do (do these by hand)

| Gap | What to do |
|---|---|
| **The reboot** | The overlay and patched module only load at boot. The script stops and tells you; re-run it afterwards. |
| **Fill the descriptor** | It drafts `configs/benches/bench-NN.yaml` with FIXMEs. Fill owner / hosts / routing, declare **only** capabilities the bench really has (labels route other people's runs), then commit and PR it. |
| **CI credentials** | Neither token is touched by any script: they are per-repo / per-org GitHub secrets — see [§5](#5-ci--the-fleet-model). A new bench needs none of its own; a new *firmware repo* needs adding to the `HIL` org secret. |
| **`can-flasher`** | External (`isc-fs/MingoCAN`), **≥ v2.8.0 mandatory**: older builds erase, then fail mid-transfer, leaving the carrier with no app (it wiped bench-01's AMS at 2.5.5). Auto-installs only if `gh` is authenticated on the Pi; otherwise install it from a workstation ([`getting-started.md` §10](docs/getting-started.md#10-install-can-flasher)). |
| **Stimulus hardware** | The **Pico LTC emulator** (cells / temps), the **NTC interposer** (temp-open faults) and the **pack-current fixture** are undocumented and outside the script. Without them a new bench can only honestly declare `dut-*` capabilities. Pico firmware: [`docs/pico_ltc_emulator.md`](docs/pico_ltc_emulator.md). |
| **Tailscale + network** | bench-01 has a Tailscale address and a fixed LAN IP; no join / static-IP step exists anywhere. Set these up yourself. |
| **Carrier bootloaders** | Each STM32 carrier needs `isc-fs/stm32-can-bootloader` burned via SWD — and its node id provisioned — out of band. The bench cannot burn a bootloader. |

**Refreshing an existing bench is not the same as a fresh install.**
`bench_setup.sh` only checks that *a* `mcp2515-triple.dtbo` is present, and
`bench doctor` only looks for `/dev/spidev0.3` — so a Pi still carrying the
pre-2026-09-04 overlay passes both while the broker silently falls back to
userspace chip-selects (the DAC-wedge race). On a refresh, recompile and
reinstall the overlay by hand, reboot, and check that
`/dev/spidev0.4`–`0.11` exist.

---

## 5. CI — the fleet model

**`.github/workflows/` contains two chains. Only Chain A is current.**

### Chain A — capability-routed (current)

Trigger: on a **firmware PR**, add the `hil-test` label or comment
`/hil-test [--bench <id>] [suite-or-path]`. Those triggers are each firmware
repo's own `hil-test.yml`, which dispatches this repo's
[`hil-test.yml`](.github/workflows/hil-test.yml):

```
resolve (ubuntu)  →  build (ubuntu, hil-fw-build.yml)  →  test (self-hosted bench)
  capabilities        firmware @ exact SHA, built from      flash under flock,
  → one bench         configs/firmware/<dut>.yaml (the      then pytest, then
  + its labels        recipe is reviewed HERE, never        comment the verdict
                      taken from the PR), layout gate,      on the PR
                      .bin uploaded as an artifact
```

- **`resolve`** maps the requested capabilities to a bench and its runner
  labels, and fails fast if no online runner carries them (GitHub would
  otherwise queue the job forever).
- **`build`** runs on `ubuntu-latest` with the recipe's pinned toolchain
  (**14.2.Rel1**). The org is on GitHub's free plan and the artifact quota
  fills up; when the upload fails, the bench **rebuilds the same reviewed
  commit locally** instead.
- **`test`** runs on the bench (`runs-on: [self-hosted, hil-bench,
  <bench-id>, <capabilities…>]`). A preflight recover ladder unwedges the
  bench first; flash and pytest then run under **one `flock`**; the verdict
  comment includes a table of failing cases.
- **Suites.** `/hil-test` alone runs `smoke`; a name resolves through
  [`configs/suites.yaml`](configs/suites.yaml); anything containing `/` or
  `::` is passed to pytest as a path. Detail:
  [`docs/development/testing.md`](docs/development/testing.md#running-a-suite-from-a-firmware-pr).

**Label vs comment — the GitHub trap.** A **label** run reads the trigger
from the *PR's own tree*. A **comment** (`issue_comment`) always reads it
from the firmware repo's **default branch**. So the trigger must be on `main`
in each firmware repo for `/hil-test` to fire at all, whatever the PR
targets. (It is, today, on both ECU and AMS.)

### Chain B — subdir-based (legacy, dead)

`hil-build-trigger.yml` (`/hil-build <subdir>`) → `hil-build-only.yml`
(Docker image, ARM GCC **12.3**) → `hil-flash.yml` (runs on `[self-hosted,
hil-rpi]`, via the legacy `tools.flash`). Superseded by Chain A. No runner
carries the `hil-rpi` label (bench-01's doesn't), so it can only queue.
**Much of the older documentation described this chain** — that was the
largest drift this refresh corrected. Slated for removal
([§7](#7-known-drift--cleanup-backlog)).

### 🔴 Credentials: two personal tokens, both single points of failure

| Secret | Lives in | Last set | Used for |
|---|---|---|---|
| **`HIL`** | **org** secret, shared to `IFS08-CE-ECU`, `IFS08-CE-AMS`, `IFS08-DV-uDV` | 2026-08-31 | The firmware repos' trigger — dispatching `hil-test.yml` into IFS_HIL. Per the trigger's own header, a fine-grained PAT with `Actions: read and write` on IFS_HIL only. |
| **`HIL_CROSS_REPO_PAT`** | IFS_HIL repo secret | 2026-03-29 | The build's firmware checkout, and the cross-repo verdict comment on the firmware PR. |

Both are **personal access tokens** — tied to one person, and they expire.
If `HIL` lapses, labels and comments stop dispatching anything. If
`HIL_CROSS_REPO_PAT` lapses, the *build* breaks too: `hil-test.yml` presents
it when checking out the firmware, and an invalid token fails the checkout
even though the firmware repos are public and need no auth at all.

- **`HIL_CROSS_REPO_PAT`** was valid on 2026-09-25 (it checked the firmware
  out in run `36193122166`). Its expiry is not visible from GitHub's secret
  list — check the token itself.
- **`HIL` needs checking now.** GitHub's UI defaults a new fine-grained PAT
  to a **30-day** lifetime. If it took the default when it was created on
  2026-08-31, it expires around **2026-09-30**.
- **Fix (recommended):** replace both with a **GitHub App** installation
  token. The org already runs one (the `DBCINATOR_APP_ID` /
  `DBCINATOR_PRIVATE_KEY` org secrets), so the pattern is established. Scope
  it to `contents:read` on the firmware repos, `actions:write` on IFS_HIL,
  and `issues:write` / `pull-requests:write` for the verdict comment. Until
  then, record who owns each token and when it expires.

---

## 6. Operating it

Full recipes: [`docs/operator-guide.md`](docs/operator-guide.md). Something
broke: [`docs/troubleshooting.md`](docs/troubleshooting.md). The short
version:

**Health check** — run on the Pi:
```sh
pi$ cd ~/IFS_HIL && python3 -m tools.bench doctor   # host matches the documented build
pi$ python3 -m tools.bench verify --bench bench-01   # hardware matches its descriptor
pi$ ls /dev/spidev0.{3..11}                          # current overlay (doctor misses this)
```

**Self-healing.** `hil-bench-watchdog.timer` (every 5 min) verifies the bench
and, if it is wedged, climbs a recover ladder: **L1** = restart `hil-broker`;
**L2** = PSU power-on reset + reload `mcp251x` + restart `hil-can-up` and
`hil-broker`. It takes the bench lock *non-blocking*, so it never
rail-cycles a carrier mid-flash, and it logs healthy checks too — a bench
that recovers every cycle is a worsening fault. Before each recovery it
appends the wedged state to `~/hil-wedge-evidence.jsonl`. It also watches
the runner: if the runner service is not active it logs `RUNNER DOWN` and
exits non-zero (it never restarts the runner itself, and a runner fault
never triggers a rail cycle). Watch it with
`journalctl -u hil-bench-watchdog -f`.

**The two recurring hardware wedges** (both auto-recovered now, but know them):
- **DAC wedge** — a DAC80504 returns a wrong device id (expected `0x0417`).
  It was a userspace chip-select race, fixed by giving all chip-selects to the
  kernel (#124). A PSU power-on reset clears any residue.
- **`can2` `mcp251x` wedge** — reconfiguring or hammering `can2` wedges the
  controller: traffic stops while `ip link` still says UP, which *looks like
  the firmware stopped transmitting*. Recover with `modprobe -r mcp251x &&
  modprobe mcp251x`, then restart `hil-can-up` and `hil-broker`. Don't
  reconfigure `can2` while a carrier is under test.

**Running a DUT suite by hand** has two traps, both covered in
[`CLAUDE.md`](CLAUDE.md#run-tests): take the bench lock
(`flock /tmp/hil-bench.lock …`) or a dispatched CI run can land mid-session;
and set `ECU_FIRMWARE_BIN` / `AMS_FIRMWARE_BIN` to your image, because
Block A's A-003 reflashes the carrier from it — and on a hand run with it
unset, the ECU suite reflashes a stale June 2026 diagnostic build that
happens to sit on bench-01.

---

## 7. Known drift / cleanup backlog

Nothing here is urgent, but a new maintainer should know it exists. Confirm
with Raúl before touching anything hardware-adjacent.

**Repo hygiene (safe to clean up):**
- **Delete the dead Chain B**: `hil-build-trigger.yml`, `hil-build-only.yml`,
  `hil-flash.yml` and the `docker/` image they use; `infra/systemd/hil-agent.service`
  (its `run_hil_job.sh` does not exist) and `configs/hil_agent.yaml`; the
  legacy `configs/ecu_*.yaml`; and the legacy `tools/flash.py` / `tools/mcp2515.py`.
  Removing Chain B also removes the ARM GCC **12.3 vs 14.2** toolchain split.
- **Make the build checks catch an old overlay**: `bench doctor` should
  require `/dev/spidev0.4`–`0.11`, and `bench_setup.sh` should compare the
  installed `.dtbo` with a fresh compile (it already does that for the
  systemd units) — see [§4](#4-standing-up-a-new-pi--bench).
- **One stale reference, left on purpose**: a comment in
  `infra/systemd/hil-broker.service` points at `docs/broker_migration_plan.md`
  (actual file: `docs/design/broker-migration.md`). `bench_setup.sh`
  compares installed units byte-for-byte, so touching it makes every bench
  re-install the unit; fix it alongside a real change to that unit.
- **Check `bench_setup.sh` on a fresh image**: its step 1 enables SPI via
  `raspi-config` whenever `config.txt` has neither the overlay nor
  `dtparam=spi=on` — always the case on a fresh Pi, since the overlay is
  installed later — although its own comment calls that parameter a second
  claimant for SPI0. Never exercised: bench-01 was built by hand.
- **Legacy udev rule**: `infra/udev/99-hil.rules` renames a USB-CAN adapter
  to `can0`, which would collide with the kernel `mcp251x` `can0`. No such
  adapter is on the bench; drop the rule.
- **Held work**: the ECU **Block K** telemetry-gate test
  (`tests/hil/vcu/test_block_k_telemetry_gate.py`, commit `3759cd8`) is on
  `feat/hil-96` — pushed 2026-09-26, byte-identical to the bench's copy,
  and it still merges cleanly into `dev`. It is held until the ECU
  telemetry firmware lands on ECU `dev`, and it needs hardening before its
  PR: its K-003/K-004 "failures" were the `can2` `mcp251x` wedge, not the
  firmware (so it must not reconfigure `can2` mid-run, and should check
  ERROR-ACTIVE), and K-001's ≤ 1.5 s boot check needs a bootloader-grace
  allowance.
- **Issues to close**: **#117** (Pico NTC pull-up) was fixed by PR #118 but
  is still open; **#94** (LOGFS pull) was reported working end-to-end on a
  quiet bus on 2026-09-25 — confirm and close.

**Process / access risks:**
- **Two personal tokens** carry all of CI (see
  [§5](#5-ci--the-fleet-model)); `HIL` may be on a 30-day clock.
- **`dev` is not branch-protected** on IFS_HIL (the protection API returns
  404), yet it is the trunk. `origin/dev` is **287 commits ahead** of
  `origin/main` (2026-09-26); `main` lacks the current workflows entirely.
  Protect `dev`; treat `main` as a release snapshot only.
- **One-person project.** Every commit on `dev` is Raúl's; no second person
  has run the bringup or the CI path. The docs are complete but unproven by
  anyone else.

**Open issues (2026-09-26):**

| # | Summary |
|---|---|
| #128 | ECU `0x703` PitDiag_fwinfo never reaches the bus (fails H-001/H-003; `telemetry` suite only, excluded from `smoke`). |
| #135 | Pico emulator: a C fall-through sets `is_wrcomm_xact` for 11 opcodes — could retarget the temperature mux. |
| #117 | Pico NTC pull-up — **fixed by PR #118, close it.** |
| #94 | AMS LOGFS pull end-to-end — reported working 2026-09-25; confirm and close. |
| #95 | AMS temp open-wire validation (`RequiredTempSlots` all-40). |
| #96 | AMS < 500 ms Error on any cell-voltage / temperature measurement loss. |

**Known flakes (not bugs):** AMS A-008 (`0x4A2` cadence, marginal) and A-012
(passes alone, fails inside a full Block A — ordering). And `can-flasher`,
even ≥ 2.8.0, occasionally NACKs `BAD_SESSION` mid-transfer; the appless
recovery path makes that survivable — retry before calling it a regression.

**Documentation gaps:** the stimulus hardware (Pico wiring, NTC interposer,
pack-current fixture) is undocumented — only bench-01's `routing` block
records it. The KiCad project `docs/BACKPLANE_HIL/` is gitignored and lives
only on Raúl's Mac.

---

## 8. First tasks for the next maintainer

In rough priority order:

1. **Check the `HIL` token's expiry today** — if it took GitHub's 30-day
   default it lapses around 2026-09-30 and every label/comment trigger dies
   with it. Record the owner and expiry of both tokens.
2. **Prove the repaired chain end-to-end.** Re-dispatch the AMS run now that
   #142 is merged (expect the flash to succeed), then open a trivial
   firmware PR and add the `hil-test` label: it must resolve → build → flash
   → comment a verdict. That label run is the only thing that exercises both
   tokens.
3. **Replace both tokens with a GitHub App** (the org already runs one).
4. **Protect `dev`** on IFS_HIL (PR + green checks required).
5. **Harden Block K, then open the `feat/hil-96` PR** once the ECU
   telemetry firmware is on ECU `dev` ([§7](#7-known-drift--cleanup-backlog)).
6. **Do a second, independent bringup** (`bench_setup.sh --bench bench-02` on
   a spare Pi) to prove the docs, and file every gap you hit — starting with
   whether `config.txt` ends up carrying `dtparam=spi=on`
   ([§7](#7-known-drift--cleanup-backlog)).
7. **Remove the dead Chain B and legacy files**, harden `doctor` against an
   old overlay, and close #117 / #94.
8. **Document the stimulus hardware** so a new bench can declare
   `stim-*` / `fault-*` capabilities.

---

## 9. Where to go next

| You want… | Read |
|---|---|
| The operator mental model + invariants | [`CLAUDE.md`](CLAUDE.md) |
| Component tour | [`docs/architecture.md`](docs/architecture.md) |
| Pin / address / netdev map | [`docs/hardware-reference.md`](docs/hardware-reference.md) → [`tools/hw_config.py`](tools/hw_config.py) |
| Stand up a Pi by hand | [`docs/getting-started.md`](docs/getting-started.md) |
| Day-to-day recipes | [`docs/operator-guide.md`](docs/operator-guide.md) |
| Something broke | [`docs/troubleshooting.md`](docs/troubleshooting.md) |
| Run or add a suite from a PR | [`docs/development/testing.md`](docs/development/testing.md) |
| Broker RPC surface | [`docs/broker-api.md`](docs/broker-api.md) |
| Dashboard HTTP API | [`docs/dashboard.md`](docs/dashboard.md) |
| systemd units + the runner drop-in | [`infra/systemd/README.md`](infra/systemd/README.md) |
| Why the kernel driver is patched | [`docs/design/mcp251x-driver-patches.md`](docs/design/mcp251x-driver-patches.md) |
| How we got here, PR by PR | [`docs/design/phase-history.md`](docs/design/phase-history.md) |
