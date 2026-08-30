#!/usr/bin/env python3
"""Bench descriptor tooling — declare, resolve, and verify a HIL bench.

Each bench in the fleet is described by one owner-authored YAML file in
`configs/benches/`, validated against `configs/benches/schema.json`.

The split that makes descriptors trustworthy:

  declared   what the owner asserts (capabilities, wiring, slot occupancy)
  detected   what `describe` probes off the live bench
  verified   what `verify` gets by diffing the two

Without the third step a descriptor is only a claim, and a stale one silently
routes a run to a bench whose emulator is unplugged.

Subcommands:

  validate                     schema-check every descriptor (offline; CI uses this)
  list                         one line per bench: id, host, capabilities
  labels   --bench ID          runner label set for that bench
  resolve  --capabilities a,b  which benches satisfy a capability set
  describe [--deep]            probe the live bench, emit JSON
  verify   --bench ID          probe and diff against the descriptor's `expect`

`validate`, `list`, `labels` and `resolve` are offline and run anywhere.
`describe` and `verify` need a broker socket (real bench, or
`python -m broker.server --fake` off-bench).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = REPO_ROOT / "configs" / "benches"
SCHEMA_PATH = BENCH_DIR / "schema.json"

# Address spaces worth probing. Kept here rather than in hw_config.py because a
# probe must be able to look for peripherals a given bench does NOT have.
INA_CANDIDATES = [0x40, 0x41, 0x44, 0x45]
TCA_CANDIDATES = [0x20, 0x21, 0x22]
DAC_CANDIDATES = [0, 1, 2, 3]

# A DAC80504 reporting device id 0x0000 is wedged, not absent -- it answers but
# has lost its POR state. Recovering it needs a full PSU cycle plus a broker
# restart, so the probe calls it out rather than reporting a healthy device.
DAC_WEDGED_ID = 0x0000


# --------------------------------------------------------------------------
# descriptor loading
# --------------------------------------------------------------------------

def _load_yaml(path):
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML is required: pip install pyyaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _normalise(desc):
    """YAML gives integer keys for unquoted slot numbers; JSON Schema only ever
    sees string keys. Normalise so an unquoted `2:` validates the same as `"2":`.
    """
    slots = desc.get("slots")
    if isinstance(slots, dict):
        desc["slots"] = {str(k): v for k, v in slots.items()}
    return desc


def load_descriptors():
    """Every descriptor in configs/benches/, keyed by bench id."""
    out = {}
    for path in sorted(BENCH_DIR.glob("*.yaml")):
        desc = _normalise(_load_yaml(path))
        if not isinstance(desc, dict) or "id" not in desc:
            sys.exit(f"{path.name}: not a bench descriptor (no `id`)")
        out[desc["id"]] = (path, desc)
    return out


def get_descriptor(bench_id):
    benches = load_descriptors()
    if bench_id not in benches:
        known = ", ".join(sorted(benches)) or "(none)"
        sys.exit(f"unknown bench '{bench_id}'. Known: {known}")
    return benches[bench_id][1]


def runner_labels(desc):
    base = desc.get("runner", {}).get("base_labels", ["self-hosted", "hil-bench"])
    return list(base) + [desc["id"]] + sorted(desc.get("capabilities", []))


# --------------------------------------------------------------------------
# broker probing
# --------------------------------------------------------------------------

def _client():
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from broker.server import BrokerClient
    except ImportError:
        sys.exit("cannot import broker.server — run from the repo root")
    sock = os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock")
    if not Path(sock).exists():
        sys.exit(
            f"no broker socket at {sock}\n"
            "  on a bench:   systemctl status hil-broker\n"
            "  off-bench:    python -m broker.server --fake --socket /tmp/hil-broker.sock\n"
            "                export HIL_BROKER_SOCKET=/tmp/hil-broker.sock"
        )
    return BrokerClient(sock)


def _try(client, method, **params):
    """Probe one method. A raised error means 'not present', which is a result,
    not a failure -- absence is exactly what we are measuring."""
    try:
        return client.call(method, **params)
    except Exception:
        return None


def _probe_can():
    """CAN facts come from the kernel, not the broker. Records the sample point
    because hil-can-up sets 0.875 while the AMS bus needs 0.6875, and the
    mismatch presents as an unexplained bus-off rather than a config error."""
    found = {}
    for dev in ("can0", "can1", "can2"):
        try:
            r = subprocess.run(["ip", "-details", "link", "show", dev],
                               capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, subprocess.SubprocessError):
            return found          # not Linux, or no iproute2 — nothing to say
        if r.returncode != 0:
            continue
        entry = {"up": " state UP " in r.stdout or "<NO-CARRIER" not in r.stdout}
        m = re.search(r"bitrate (\d+)", r.stdout)
        if m:
            entry["bitrate"] = int(m.group(1))
        m = re.search(r"sample-point ([\d.]+)", r.stdout)
        if m:
            entry["sample_point"] = float(m.group(1))
        found[dev] = entry
    return found


def probe(deep=False):
    """Passive inventory of the live bench.

    Passive by default: presence checks only, no relay actuation, so probing
    somebody else's bench cannot disturb what it is doing. `deep` additionally
    powers each carrier slot to see which are populated, which is intrusive and
    therefore opt-in.
    """
    client = _client()
    result = {"psu_ok": None, "broker": None, "ina": [], "tca": [],
              "dac": [], "dac_wedged": [], "nrf24": False, "can": {},
              "slots_powered": None, "warnings": []}

    result["broker"] = _try(client, "broker.health")

    psu = _try(client, "psu.status")
    if isinstance(psu, dict):
        result["psu_ok"] = bool(psu.get("pwr_ok"))
    if result["psu_ok"] is False:
        # Invariant #6: IC1's ~OE is gated by ATX PWR_OK, so every SPI
        # peripheral reads absent while the PSU is off. Probing anyway would
        # write a descriptor claiming the bench has no DACs.
        result["warnings"].append(
            "PSU is off — SPI peripherals (DAC) read as absent. "
            "Power the bench before trusting this probe.")

    for addr in INA_CANDIDATES:
        if _try(client, "ina.is_present", addr=addr):
            result["ina"].append(addr)
    for addr in TCA_CANDIDATES:
        if _try(client, "tca.is_present", addr=addr):
            result["tca"].append(addr)
    for idx in DAC_CANDIDATES:
        dev_id = _try(client, "dac.read_device_id", idx=idx)
        if dev_id is None:
            continue
        result["dac"].append(idx)
        if dev_id == DAC_WEDGED_ID:
            result["dac_wedged"].append(idx)

    if result["dac_wedged"]:
        result["warnings"].append(
            f"DAC(s) {result['dac_wedged']} report device id 0x0000 — wedged. "
            "Recover with a full PSU off/on cycle plus `systemctl restart hil-broker`.")

    result["nrf24"] = bool(_try(client, "nrf.is_present"))
    result["can"] = _probe_can()

    if deep:
        result["slots_powered"] = _probe_slots(client)

    return result


def _probe_slots(client):
    """Which carrier slots actually have a board seated.

    Intrusive: closes each carrier relay in turn and reads that slot's INA. ~130
    mA means a carrier is alive; <=1 mA means the relay closed onto nothing (or
    a blown fuse). Slots are restored to their prior state on the way out.
    """
    ina_for_slot = dict(zip((1, 2, 3, 4), INA_CANDIDATES))
    found = {}
    before = _try(client, "tca.read_port", addr=0x20, port=0)
    for slot, ina_addr in ina_for_slot.items():
        pin = slot - 1
        _try(client, "tca.set_direction", addr=0x20, port=0, mask=0x00)
        _try(client, "tca.write_pin", addr=0x20, port=0, pin=pin, value=True)
        current = _try(client, "ina.current", addr=ina_addr)
        found[str(slot)] = {
            "current_mA": round(current * 1000, 1) if current is not None else None,
            "populated": bool(current is not None and current > 0.010),
        }
    if before is not None:
        _try(client, "tca.write_port", addr=0x20, port=0, value=before)
    return found


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_validate(args):
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        sys.exit("jsonschema is required: pip install jsonschema")

    schema = json.loads(SCHEMA_PATH.read_text())
    validator = Draft202012Validator(schema)
    benches = load_descriptors()
    if not benches:
        sys.exit(f"no descriptors found in {BENCH_DIR}")

    failures = 0
    for bench_id, (path, desc) in sorted(benches.items()):
        errors = sorted(validator.iter_errors(desc), key=lambda e: list(e.path))
        # The filename is how a human finds a bench; keeping it in step with the
        # id stops "edit bench-02.yaml" from silently changing bench-03.
        if path.stem != bench_id:
            errors.append(type("E", (), {
                "json_path": "$.id",
                "message": f"id '{bench_id}' does not match filename '{path.name}'",
            })())
        if errors:
            failures += 1
            print(f"✗ {path.name}")
            for e in errors:
                loc = getattr(e, "json_path", "$")
                print(f"    {loc}: {e.message}")
        else:
            caps = len(desc.get("capabilities", []))
            print(f"✓ {path.name}  ({caps} capabilities)")

    print(f"\n{len(benches) - failures}/{len(benches)} descriptor(s) valid")
    return 1 if failures else 0


def cmd_list(args):
    for bench_id, (_, desc) in sorted(load_descriptors().items()):
        hosts = desc.get("hosts", {})
        host = hosts.get("lan") or hosts.get("tailscale") or "?"
        print(f"{bench_id}  {host}")
        print(f"    owner : {desc.get('owner', {}).get('github', '?')}")
        print(f"    caps  : {' '.join(sorted(desc.get('capabilities', [])))}")
    return 0


def cmd_labels(args):
    print(",".join(runner_labels(get_descriptor(args.bench))))
    return 0


def cmd_resolve(args):
    """Match a capability set to benches. This is what the dispatch workflow
    calls; it runs on a cloud runner with no bench access, which is why the
    inventory has to live in the repo rather than being served by the benches."""
    required = {c.strip() for c in args.capabilities.split(",") if c.strip()}
    benches = load_descriptors()

    matches = []
    for bench_id, (_, desc) in sorted(benches.items()):
        caps = set(desc.get("capabilities", []))
        if required <= caps:
            matches.append((bench_id, desc))

    if not matches:
        # Fail with the diff, not just "no match" -- an actionable error is the
        # difference between fixing it now and opening an issue about it.
        print(f"no bench satisfies: {' '.join(sorted(required))}\n", file=sys.stderr)
        for bench_id, (_, desc) in sorted(benches.items()):
            missing = required - set(desc.get("capabilities", []))
            print(f"  {bench_id} lacks: {' '.join(sorted(missing))}", file=sys.stderr)
        return 1

    chosen_id, chosen = matches[0]
    payload = {
        "bench": chosen_id,
        "labels": runner_labels(chosen),
        "candidates": [m[0] for m in matches],
    }
    if args.github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"bench={chosen_id}\n")
            f.write(f"labels={json.dumps(payload['labels'])}\n")
    print(json.dumps(payload, indent=2))
    return 0


def _capability_menu():
    """Every legal capability, straight from the schema, so the menu in a draft
    can never drift from what `validate` accepts."""
    schema = json.loads(SCHEMA_PATH.read_text())
    return schema["properties"]["capabilities"]["items"]["enum"]


def _draft_yaml(bench_id, found):
    """A starter descriptor: probe results filled in, everything a probe cannot
    know left for the owner. Deliberately incomplete -- `capabilities` is empty
    so `validate` fails until a human states what this bench can actually do.
    Guessing capabilities on someone's behalf is how a fleet ends up routing to
    a bench that cannot do the job.
    """
    hexes = lambda xs: "[" + ", ".join(f"0x{v:02X}" for v in xs) + "]"

    slot_lines = []
    for slot, ina_addr in zip((1, 2, 3, 4), INA_CANDIDATES):
        if ina_addr not in found["ina"]:
            continue
        seated = (found.get("slots_powered") or {}).get(str(slot), {})
        hint = ""
        if seated:
            hint = ("  # carrier detected, {:.0f} mA".format(seated["current_mA"])
                    if seated.get("populated") else "  # no carrier seated")
        slot_lines.append(f'  "{slot}": {{ dut: none, ina: 0x{ina_addr:02X} }}{hint}')
    slots_block = "\n".join(slot_lines) or '  "1": { dut: none }'

    can_lines = []
    for dev, info in sorted(found["can"].items()):
        can_lines.append(f"  {dev}:")
        can_lines.append(f"    dev: {dev}")
        can_lines.append(f"    bitrate: {info.get('bitrate', 500000)}")
        if "sample_point" in info:
            can_lines.append(f"    sample_point: {info['sample_point']}")
    can_block = "\n".join(can_lines) or (
        "  acu:\n    dev: can2\n    bitrate: 500000\n    sample_point: 0.6875")

    menu = "\n".join(f"#   - {c}" for c in _capability_menu())

    return f'''# Draft descriptor for {bench_id}, generated by:
#   python -m tools.bench describe --draft {bench_id}
#
# Values under `expect`, `can` and `slots` came from a live probe of this
# bench. Everything else is yours to fill in -- no probe can tell which DUT is
# in which slot, what an interposer is wired to, or who to call when the bench
# is down.
#
# Then:  python -m tools.bench validate
#        python -m tools.bench verify --bench {bench_id}

schema_version: 1
id: {bench_id}

owner:
  name: FIXME
  github: FIXME

board_rev: FIXME            # e.g. BACKPLANE_HIL-A
location: FIXME

hosts:
  lan: FIXME                # user@host
  # tailscale: user@host    # recommended: lets the fleet reach it off-LAN

runner:
  base_labels: [self-hosted, hil-bench]

# What this bench can DO. Left empty on purpose: `validate` will fail until you
# declare it, because a wrong guess here routes other people's tests to a bench
# that cannot run them. Name the capability, never the hardware -- `pico-ltc`
# is rejected, `stim-cells` is what you want.
#
# Choose from:
{menu}
capabilities: []

# What physically provides each capability. Documentation only, never routed on,
# so swapping an emulator changes nothing for tests. Free-form values.
hardware:
  cell_temp_emulator: FIXME     # e.g. pico-ltc, real-pack
  pack_current_source: FIXME    # e.g. dac80504
  temp_fault: FIXME             # e.g. ntc-interposer, none

can:
{can_block}

# Detected from which INA226s answered. Set `dut` for each occupied slot
# (ams | ecu | udv | none).
slots:
{slots_block}

# Bench-physical wiring, so no test ever hardcodes a DAC index or TCA pin.
routing:
  pack_current: {{ dac: FIXME, ch_p: FIXME, ch_n: FIXME }}
  tsms:         {{ tca: FIXME, port: FIXME, pin: FIXME }}

# Probed. `verify` re-probes and diffs against this.
expect:
  ina:  {hexes(found["ina"])}
  tca:  {hexes(found["tca"])}
  dac:  {found["dac"]}
  nrf24: {str(found["nrf24"]).lower()}
'''


def cmd_describe(args):
    result = probe(deep=args.deep)
    text = _draft_yaml(args.draft, result) if args.draft \
        else json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text if args.draft else text + "\n")
        print(f"wrote {args.out}")
    else:
        print(text)
    for w in result["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_verify(args):
    desc = get_descriptor(args.bench)
    expect = desc.get("expect", {})
    if not expect:
        print(f"{args.bench} declares no `expect` block — nothing to verify.\n"
              "Run `describe` on the bench and add one.", file=sys.stderr)
        return 1

    found = probe(deep=False)
    problems = []

    for key in ("ina", "tca", "dac"):
        want, got = set(expect.get(key, [])), set(found[key])
        if want - got:
            problems.append(f"{key}: declared but missing → "
                            f"{sorted(hex(a) if key != 'dac' else a for a in want - got)}")
        if got - want:
            problems.append(f"{key}: present but undeclared → "
                            f"{sorted(hex(a) if key != 'dac' else a for a in got - want)}")

    if "nrf24" in expect and expect["nrf24"] != found["nrf24"]:
        problems.append(f"nrf24: declared {expect['nrf24']}, detected {found['nrf24']}")

    # A declared sample point that the live bus contradicts is the single
    # highest-value check here: it is invisible until a DUT mysteriously bus-offs.
    for role, spec in desc.get("can", {}).items():
        dev = spec.get("dev")
        live = found["can"].get(dev)
        if not live:
            problems.append(f"can[{role}]: {dev} not present on this host")
            continue
        for field in ("bitrate", "sample_point"):
            if field in spec and field in live and spec[field] != live[field]:
                problems.append(
                    f"can[{role}] {dev}: declared {field}={spec[field]}, "
                    f"live {field}={live[field]}")

    for w in found["warnings"]:
        print(f"warning: {w}", file=sys.stderr)

    if problems:
        print(f"✗ {args.bench} does not match its descriptor:")
        for p in problems:
            print(f"    {p}")
        return 1

    print(f"✓ {args.bench} matches its descriptor "
          f"({len(desc.get('capabilities', []))} capabilities declared)")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="schema-check every descriptor").set_defaults(
        func=cmd_validate)
    sub.add_parser("list", help="list benches and capabilities").set_defaults(
        func=cmd_list)

    p = sub.add_parser("labels", help="runner label set for a bench")
    p.add_argument("--bench", required=True)
    p.set_defaults(func=cmd_labels)

    p = sub.add_parser("resolve", help="which bench satisfies a capability set")
    p.add_argument("--capabilities", required=True,
                   help="comma-separated, e.g. dut-ams,stim-temps")
    p.add_argument("--github-output", action="store_true",
                   help="also append bench/labels to $GITHUB_OUTPUT")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("describe", help="probe the live bench")
    p.add_argument("--deep", action="store_true",
                   help="also power each slot to detect seated carriers (INTRUSIVE)")
    p.add_argument("--draft", metavar="BENCH_ID",
                   help="emit a starter descriptor for this bench id instead of JSON")
    p.add_argument("--out", help="write here instead of stdout")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("verify", help="probe and diff against the descriptor")
    p.add_argument("--bench", required=True)
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
