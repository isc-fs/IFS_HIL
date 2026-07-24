"""
Block U -- microSD datalogger MULTI-RUN validation (IFS08_HIL #91).

#91 asked: exercise the MLC2 microSD logger across >= 3 boot cycles
(>= 60 s each), confirm each run is retrievable as its own sealed,
non-empty log file, and that the SD activity never disturbs the isoSPI
BMS -- retrieved by READING THE CARD, because logger state isn't on CAN
(sd_log_stats() is never emitted; LOGFS-over-CAN has wire mismatches,
#452 / can-flasher#506).

Split, same as Block T, by what's observable without a card pull:

| ID    | Check                                                          | Runs on   |
|-------|----------------------------------------------------------------|-----------|
| U-160 | across N reboots + full-duration logging the module mask       | std rig   |
|       | (0x4A0 b2) never drops -- the churning logger must not perturb  |           |
|       | the isoSPI BMS read                                             |           |
| U-161 | file-per-run + FRAGMENTATION guard: a >= 60 s run is ONE sealed | CARD PULL |
|       | ~240-row file, not hundreds of 1-2 row shards                  |           |
| U-162 | no truncation / #448: distinct monotonic indices, each sealed  | CARD PULL |
|       | .CSV has a .CRC sidecar, card mask is full                      |           |

U-161 is the regression guard for the dev-2.1.0 time-rotation underflow
(isc-fs/IFS08-CE-AMS#495): `now - g_file_open_ms` underflows because
`now` is sampled before the multi-hundred-ms SD open that sets
`g_file_open_ms`, so `should_rotate()` seals every drain. Confirmed on
the MLC2 bench 2026-07-24: fresh-wiped card, 3x ~65 s runs -> 990 sealed
files (mostly 1-2 rows), 0 orphans. A correct build writes ~1 file/run.

Logger config (ams_config.hpp): 4 Hz (LogSamplePeriodMs=250), rotate at
4 MiB OR 5 min, LOG%04lu.TMP -> .CSV + .CRC sidecar.

CARD-PULL steps (U-161/U-162): wipe the card to a fresh FAT32 baseline,
run this block with AMS_SD_MULTIRUN_BOOTS reboots, power off, pull the
card, mount it, and set AMS_SD_MOUNT to the mount point, e.g.:

    AMS_SD_MULTIRUN_BOOTS=3 AMS_SD_MULTIRUN_SOAK_S=60 \
        pytest tests/hil/ams/test_block_u_microsd_multirun.py::TestBlockUMultiRun::test_u160_mask_stable_across_reboots
    # ... power off, pull card, mount ...
    AMS_SD_MOUNT=/media/AMSLOG pytest tests/hil/ams/test_block_u_microsd_multirun.py -k card
"""

from __future__ import annotations

import glob
import os
import statistics
import time

import pytest

from tools.firmware_test.ams import can_map as M

_SOAK_S = float(os.environ.get("AMS_SD_MULTIRUN_SOAK_S", "60"))
_N_BOOTS = int(os.environ.get("AMS_SD_MULTIRUN_BOOTS", "3"))
_CARD_MOUNT = os.environ.get("AMS_SD_MOUNT")     # operator pulls + mounts the card

_SAMPLE_PERIOD_MS = 250                            # LogSamplePeriodMs (4 Hz)
_TCA_ADDR = 0x20                                   # carrier-relay TCA9555
_TRANSIENT_S = 2.5                                 # skip the boot-grace ramp


def _expected_rows(soak_s: float) -> int:
    """Rows a healthy run of `soak_s` seconds writes (one file, 4 Hz)."""
    return int(soak_s * 1000 / _SAMPLE_PERIOD_MS)


def _reboot(client, relay_bit, observe_acu, timeout_s: float = 12.0):
    """Power-cycle the MLC slot and block until the app's first 0x4A0."""
    observe_acu.clear()
    client.call("tca.write_pin", addr=_TCA_ADDR, port=0, pin=relay_bit, value=False)
    time.sleep(0.6)
    client.call("tca.write_pin", addr=_TCA_ADDR, port=0, pin=relay_bit, value=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if observe_acu.last(M.ID_TELEM_STATUS, extended=False) is not None:
            return
        time.sleep(0.05)
    pytest.fail(f"no 0x4A0 within {timeout_s:.0f}s after power-cycle -- "
                "carrier/BL didn't come up (bench/relay fault, not firmware)")


def _read_card(mount: str) -> list[dict]:
    """Parse LOGnnnn.CSV on a mounted card into per-file summaries.

    Returns dicts with idx, rows (data rows, header excluded), a `crc`
    flag (does the LOGnnnn.CRC sidecar exist), and `mask_full` (every
    non-transient row reports the full module mask).
    """
    out: list[dict] = []
    for f in sorted(glob.glob(os.path.join(mount, "LOG*.CSV"))):
        base = os.path.basename(f)
        try:
            idx = int(base[3:7])
        except ValueError:
            continue
        with open(f, "r", errors="replace") as fh:
            lines = fh.read().splitlines()
        rows = lines[1:] if lines else []
        masks = []
        for r in rows:
            c = r.split(",")
            if len(c) > 8:
                try:
                    masks.append(int(c[8]))
                except ValueError:
                    pass
        out.append({
            "idx": idx,
            "rows": len(rows),
            "crc": os.path.exists(f[:-4] + ".CRC"),
            "masks": masks,
            "path": f,
        })
    return out


class TestBlockUMultiRun:

    # U-160 -- the churning logger must not drop an isoSPI module ------------
    def test_u160_mask_stable_across_reboots(self, fresh_boot, mlc_powered,
                                             observe_acu):
        """Across N boots, each logging for _SOAK_S, the 0x4A0 module-online
        mask must reach its full value and never drop a module. The logger is
        best-effort and off the safety path (#408); this is the #91 guarantee
        that even a badly-behaved logger (see AMS#495 fragmentation) can't
        perturb the isoSPI read. Asserts on the mask, not FSM state -- the
        mask is BMS health and is valid even when the pack-current sensor
        holds the FSM in Error."""
        from broker.server import BrokerClient

        client = BrokerClient(
            os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))
        relay_bit = mlc_powered["relay_bit"]
        full_seen = None
        try:
            for boot in range(_N_BOOTS):
                if boot > 0:                       # boot 0 == the fresh_boot fixture
                    _reboot(client, relay_bit, observe_acu)
                observe_acu.clear()
                time.sleep(_SOAK_S)
                frames = observe_acu.frames(M.ID_TELEM_STATUS)

                # cadence: the 10 ms MainTask owns 0x4A0 @ 500 ms; the logger
                # must never starve it.
                n_min = int(_SOAK_S * 1000 / M.TX_TELEM_PERIOD_MS) - 3
                assert len(frames) >= n_min, (
                    f"boot {boot}: 0x4A0 dropped ({len(frames)} vs ~{n_min + 3}) "
                    "-- logger perturbing telemetry cadence?")

                masks = [M.decode_telem_status(f.data)["module_online_mask"]
                         for f in frames]
                online = [m for m in masks if m != 0]  # drop boot-grace zeros
                assert online, f"boot {boot}: no module ever came online"
                full = max(online)
                if full_seen is None:
                    full_seen = full
                dropped = sorted({m for m in online if m != full})
                assert not dropped, (
                    f"boot {boot}: module mask fell from 0x{full:02X} to "
                    f"{[f'0x{d:02X}' for d in dropped]} mid-logging -- SD "
                    "activity perturbed the isoSPI BMS read (#91 / AMS#495).")
                assert full == full_seen, (
                    f"boot {boot}: full mask 0x{full:02X} != boot-0 "
                    f"0x{full_seen:02X} -- a module failed to enumerate.")
        finally:
            client.close()

    # U-161 -- file-per-run + fragmentation guard (operator pulls the card) --
    @pytest.mark.skipif(
        not _CARD_MOUNT,
        reason="card-pull step. Wipe the card, run test_u160 with "
               "AMS_SD_MULTIRUN_BOOTS boots, power off, pull + mount the card, "
               "then set AMS_SD_MOUNT to the mount point. Asserts each >=60 s "
               "run is ONE sealed ~240-row file (fresh-file-per-boot), NOT the "
               "hundreds of 1-2 row shards produced by the AMS#495 rotation "
               "underflow.")
    def test_u161_file_per_run_no_fragmentation(self):
        files = _read_card(_CARD_MOUNT)
        assert files, f"no LOG*.CSV under {_CARD_MOUNT} -- wrong mount, or the " \
                      "logger never wrote (check SDMMC1 MspInit, #407)."

        exp = _expected_rows(_SOAK_S)
        rows = [f["rows"] for f in files]
        max_rows = max(rows)
        median_rows = int(statistics.median(rows))
        run_files = [f for f in files if f["rows"] >= 0.25 * exp]

        # (1) FRAGMENTATION: on a wiped card, ~1 file per boot. AMS#495 turns a
        #     single run into hundreds of files.
        assert len(files) <= _N_BOOTS + 2, (
            f"{len(files)} sealed files for {_N_BOOTS} boots -- expected "
            f"~{_N_BOOTS} (fresh-file-per-boot). FRAGMENTATION regression "
            f"(AMS#495): should_rotate() age underflow seals every drain.")

        # (2) each run must be ONE substantial file (~exp rows), not shards.
        assert max_rows >= 0.5 * exp, (
            f"largest file is only {max_rows} rows; a {_SOAK_S:.0f} s run "
            f"should be ~{exp}. FRAGMENTATION regression (AMS#495).")
        assert median_rows >= 0.5 * exp, (
            f"median file is {median_rows} rows (~{exp} expected) -- runs are "
            f"being shredded into tiny files (AMS#495).")

        # (3) every boot's run is retrievable as its own non-empty file.
        assert len(run_files) >= _N_BOOTS, (
            f"only {len(run_files)} run-sized files (>= {int(0.25 * exp)} rows) "
            f"for {_N_BOOTS} boots -- each run must be its own retrievable log.")

    # U-162 -- no truncation / #448 + card-side mask health ------------------
    @pytest.mark.skipif(
        not _CARD_MOUNT,
        reason="card-pull step (see test_u161). Verifies #448: distinct "
               "monotonic indices, a .CRC sidecar next to every sealed .CSV, "
               "and the logged module mask is full for all steady-state rows.")
    def test_u162_no_truncation_and_sealed(self):
        files = _read_card(_CARD_MOUNT)
        assert files, f"no LOG*.CSV under {_CARD_MOUNT}"

        # #448: indices are unique + monotonic -- no run overwrote another.
        idxs = [f["idx"] for f in files]
        assert len(idxs) == len(set(idxs)), \
            f"duplicate LOG indices {sorted(idxs)} -- a run overwrote another (#448)."
        assert idxs == sorted(idxs), "LOG indices not monotonic -- index-scan bug (#448)."

        # every non-empty sealed .CSV carries its .CRC sidecar.
        missing_crc = [f"LOG{f['idx']:04d}" for f in files
                       if f["rows"] > 0 and not f["crc"]]
        assert not missing_crc, \
            f"sealed .CSV without a .CRC sidecar: {missing_crc} (seal_file bug)."

        # card-side BMS health: full module mask on all steady-state rows.
        online = [m for f in files for m in f["masks"] if m != 0]
        assert online, "no online-module rows on the card"
        full = max(online)
        dropped = sorted({m for m in online if m != full})
        assert not dropped, (
            f"card shows module mask dropping from 0x{full:02X} to "
            f"{[f'0x{d:02X}' for d in dropped]} -- SD write perturbed isoSPI "
            "(#91). (Excludes the per-boot transient mask==0 first rows.)")
