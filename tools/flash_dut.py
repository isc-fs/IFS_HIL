#!/usr/bin/env python3
"""Flash a DUT on a bench, driven by the bench descriptor and the DUT profile.

    python3 -m tools.flash_dut --dut ams --bin fw.bin [--bench bench-01]
    python3 -m tools.flash_dut --dut ams --bin fw.bin --dry-run

Why this exists rather than `hil-flash.yml` + `tools/flash.py`:

  * `tools/flash.py` speaks CAN ids 0x21/0x22 at a bootloader that is ISO-TP and
    node-addressed, and it returns success WITHOUT WRITING when the stored
    version matches its hardcoded default (tools/flash.py:362-374). A hardware
    gate that reports green without flashing is worse than no gate.
  * The real, exercised path is the `can-flasher` CLI (isc-fs/MingoCAN), which
    every live bench test already uses via tools/firmware_test/flash_helper.py.

The bench is left ISOLATED on the target DUT unless --restore-relays is given:
a run started from the ECU repo powers the ECU carrier and nothing else.

Everything bench-specific is resolved, never hardcoded: the carrier slot and its
relay come from configs/benches/<id>.yaml, and the node id, app address and boot
trigger come from the DUT profile.

THE SAFETY PROPERTY THAT MATTERS: the AMS and the ECU bootloaders BOTH answer on
node 0x01, and are told apart only by their boot-trigger payload. If two carriers
are powered, `discover` can answer from the wrong board and the wrong firmware
gets written. So every other DUT-bearing slot is de-energised first, and the
discovery result is asserted to be exactly one node.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.bench import load_bench, slot_for_dut          # noqa: E402

# Slot -> INA226 address. Declared per-slot in the descriptor; this is only the
# fallback for a descriptor that omits it.
INA_FOR_SLOT = {1: 0x40, 2: 0x41, 3: 0x44, 4: 0x45}

PROFILE_FOR_DUT = {
    "ams": "tests/hil/ams/ams_profile.yaml",
    # the directory is `vcu` for historical reasons; the canonical DUT name is `ecu`
    "ecu": "tests/hil/vcu/vcu_profile.yaml",
}


class FlashError(RuntimeError):
    pass


def _yaml(path):
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


def _client():
    from broker.server import BrokerClient
    sock = os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock")
    if not Path(sock).exists():
        raise FlashError(f"no broker socket at {sock} — is hil-broker running?")
    return BrokerClient(sock)


def _run(cmd, timeout, dry):
    if dry:
        print(f"    would run: {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve(dut, bench_id=None):
    """(descriptor, profile, slot, wiring) for this DUT on this bench."""
    desc = load_bench(bench_id)
    cap = f"dut-{dut}"
    if cap not in desc.get("capabilities", []):
        raise FlashError(
            f"{desc['id']} does not declare {cap}; it has "
            f"{' '.join(sorted(desc.get('capabilities', []))) or '(none)'}")
    if dut not in PROFILE_FOR_DUT:
        raise FlashError(f"no DUT profile known for '{dut}'")
    profile = _yaml(REPO_ROOT / PROFILE_FOR_DUT[dut])
    slot = slot_for_dut(desc, dut)
    return desc, profile, slot


def carrier_power(desc):
    r = (desc.get("routing") or {}).get("carrier_power") or {}
    return r.get("tca", 0x20), r.get("port", 0)


def other_dut_slots(desc, target_slot):
    """Every OTHER slot that seats a DUT. These must be dark before we flash:
    both bootloaders answer on node 0x01."""
    out = []
    for s, spec in (desc.get("slots") or {}).items():
        if int(s) != target_slot and spec.get("dut") not in (None, "none"):
            out.append((int(s), spec["dut"]))
    return sorted(out)


def flash(dut, bin_path, bench_id=None, dry=False, expect_sha=None,
          restore_relays=False):
    bin_path = Path(bin_path).resolve()
    if not bin_path.is_file():
        raise FlashError(f"image not found: {bin_path}")
    actual = sha256(bin_path)
    if expect_sha and actual != expect_sha:
        raise FlashError(
            f"image sha256 mismatch — the artifact is not what was built\n"
            f"  expected {expect_sha}\n  actual   {actual}")

    desc, profile, slot = resolve(dut, bench_id)
    bus = desc["can"].get("bms_bl", desc["can"]["acu"])["dev"]
    tca, port = carrier_power(desc)
    ina = (desc.get("slots") or {}).get(str(slot), {}).get("ina") or INA_FOR_SLOT[slot]
    node = profile["bl_node_id"]
    others = other_dut_slots(desc, slot)

    print(f"bench   : {desc['id']}")
    print(f"dut     : {dut} in MLC{slot}  (node 0x{node:02X}, bus {bus})")
    print(f"image   : {bin_path.name}  {bin_path.stat().st_size} B  sha256 {actual[:16]}")
    if dry:
        print("*** DRY RUN — nothing is energised, triggered or written ***")

    client = None if dry else _client()
    before = None
    result = {"bench": desc["id"], "dut": dut, "slot": slot,
              "image": bin_path.name, "sha256": actual, "flashed": False}

    try:
        # --- carrier power ------------------------------------------------
        # set_direction FIRST: tools/tca9555.py only writes the OUTPUT register,
        # so on a power-on-reset expander (all pins inputs) writing a pin is a
        # silent no-op and the relay never closes.
        print(f"power   : TCA 0x{tca:02X} port {port}; making pins outputs first")
        if not dry:
            before = client.call("tca.read_port", addr=tca, port=port)
            client.call("tca.set_direction", addr=tca, port=port, mask=0x00)

        for s, other in others:
            print(f"          de-energising MLC{s} ({other}) — its BL also answers node 0x01")
            if not dry:
                client.call("tca.write_pin", addr=tca, port=port, pin=s - 1, value=False)
        if others and not dry:
            time.sleep(0.3)

        print(f"          energising MLC{slot}")
        if not dry:
            client.call("tca.write_pin", addr=tca, port=port, pin=slot - 1, value=True)
            time.sleep(float(profile.get("mlc_boot_settle_s", 0.5)))
            mA = client.call("ina.current", addr=ina) * 1000
            floor = float(profile.get("mlc_boot_current_mA", 100))
            print(f"          MLC{slot} draws {mA:.1f} mA (floor {floor:.0f})")
            if mA < floor:
                raise FlashError(
                    f"MLC{slot} drew only {mA:.1f} mA — carrier not seated, or fuse blown")

        # --- into the bootloader ------------------------------------------
        trig_id = int(profile.get("bl_trigger_id", 0x002))
        payload = profile["bl_trigger_payload"]
        frame = f"{trig_id:03X}#{payload}"
        print(f"trigger : cansend {bus} {frame}")
        _run(["cansend", bus, frame], timeout=5, dry=dry)
        if not dry:
            time.sleep(1.0)

        # --- discovery: exactly one node, and the right one ----------------
        disc = ["can-flasher", "--interface", "socketcan", "--channel", bus,
                "--bitrate", str(desc["can"]["acu"]["bitrate"]),
                "--node-id", hex(node),
                "--timeout", str(profile.get("bl_discover_timeout_ms", 3000)),
                "discover"]
        print(f"discover: {' '.join(disc)}")
        r = _run(disc, timeout=30, dry=dry)
        if not dry:
            found = [l.strip() for l in r.stdout.splitlines()
                     if l.lstrip().lower().startswith("0x")]
            if not found:
                raise FlashError(
                    "no bootloader answered. The carrier may be running an app that "
                    "ignores the trigger, or it is not powered.")
            if len(found) > 1:
                raise FlashError(
                    f"{len(found)} nodes answered discover: {found}. Another carrier is "
                    "still powered — refusing to flash, this is how the wrong board "
                    "gets written.")
            row = found[0]
            print(f"          one node: {row}")
            # Identity gate. Stronger than the node id, which is provisioned into
            # flash separately from the firmware constant and does drift: on
            # bench-01 the AMS bootloader answers 0x01 while ams_config.hpp
            # declares AmsNodeId = 0x02. The product string is what the board
            # says it IS, so it catches a mis-slotted or mis-provisioned carrier
            # that a node-id check would wave through.
            want_product = profile.get("bl_product")
            if want_product and want_product not in row:
                raise FlashError(
                    f"the bootloader that answered is not {want_product}:\n"
                    f"    {row}\n"
                    f"  Refusing to flash {dut} firmware onto it.")
            if want_product:
                print(f"          identity confirmed: {want_product}")

        # --- write ---------------------------------------------------------
        addr = profile["app_flash_address"]
        cmd = ["can-flasher", "--interface", "socketcan", "--channel", bus,
               "--bitrate", str(desc["can"]["acu"]["bitrate"]),
               "--node-id", hex(node),
               "--timeout", str(profile.get("bl_per_frame_timeout_ms", 30000)),
               "flash", str(bin_path),
               "--address", f"0x{addr:08X}" if isinstance(addr, int) else str(addr),
               # --verify-after is not optional: it is the only thing that makes
               # "flashed" mean "these bytes are on the chip".
               "--verify-after", "--yes", "--jump"]
        print(f"flash   : {' '.join(cmd)}")
        r = _run(cmd, timeout=int(profile.get("bl_flash_timeout_s", 60)) + 120, dry=dry)
        if not dry:
            if r.returncode != 0:
                tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
                raise FlashError("can-flasher failed:\n    " + "\n    ".join(tail))
            print("          written and verified")
            result["flashed"] = True

    finally:
        # By default the bench is left ISOLATED: target powered, every other DUT
        # carrier dark. A run started from the ECU repo should exercise the ECU
        # alone, and restoring the snapshot here would re-energise the AMS the
        # instant the ECU flash finished -- putting a second node on the bus for
        # the whole test.
        #
        # --restore-relays opts back into the snapshot for the cases where the
        # DUTs genuinely need each other: the AMS trips VcuStale without the
        # ECU's continuous 0x100, so an AMS FSM suite wants the ECU powered.
        if restore_relays and client is None:
            print("restore : would re-energise the previously powered carriers")
        elif client is None:
            kept = ", ".join(f"MLC{s}" for s, _ in others) or "none"
            print(f"isolate : would leave MLC{slot} powered; still dark: {kept}")
        elif before is not None and restore_relays:
            try:
                client.call("tca.write_port", addr=tca, port=port, value=before)
                print(f"restore : relay port back to 0x{before:02X}")
            except Exception as exc:                       # noqa: BLE001
                print(f"restore : FAILED to restore relays: {exc}", file=sys.stderr)
        elif client is not None:
            kept = ", ".join(f"MLC{s}" for s, _ in others) or "none"
            print(f"isolate : left MLC{slot} powered; still dark: {kept}")

    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dut", required=True, choices=sorted(PROFILE_FOR_DUT))
    ap.add_argument("--bin", required=True)
    ap.add_argument("--bench")
    ap.add_argument("--expect-sha256")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore-relays", action="store_true",
                    help="re-energise the carriers that were on before. Default is "
                         "to leave the bench isolated on the target DUT, which is "
                         "what a single-DUT suite wants; use this when the suite "
                         "needs another carrier alive (an AMS FSM run needs the "
                         "ECU's 0x100 or it trips VcuStale).")
    ap.add_argument("--out", help="write a JSON record of the flash here")
    args = ap.parse_args()

    try:
        res = flash(args.dut, args.bin, args.bench, args.dry_run,
                    args.expect_sha256, args.restore_relays)
    except FlashError as exc:
        print(f"\n!! {exc}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=2) + "\n")
    print("\nOK" if res["flashed"] else "\ndry run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
