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
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = REPO_ROOT / "configs" / "benches"
SCHEMA_PATH = BENCH_DIR / "schema.json"

# Address spaces worth probing. Kept here rather than in hw_config.py because a
# probe must be able to look for peripherals a given bench does NOT have.
INA_CANDIDATES = [0x40, 0x41, 0x44, 0x45]
TCA_CANDIDATES = [0x20, 0x21, 0x22]
DAC_CANDIDATES = [0, 1, 2, 3]

# A healthy DAC80504 returns 0x0417 in the low 14 bits of DEVID (tools/dac80504.py,
# asserted by tests/hil/test_spi_dac.py). Anything else means the SPI read did not
# land: 0x0000 and 0x3FFF are the floating-low and floating-high patterns, and on
# a real bench they have been observed alternating between reads. Testing only for
# one of them reports a healthy device half the time, so compare against the
# expected value instead.
DAC_DEVID_EXPECTED = 0x0417

# `ip` reports the sample point to three decimals and the kernel quantises to
# the achievable bit timing, so declared and live never compare exactly equal.
SAMPLE_POINT_TOL = 0.005


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


def load_bench(bench_id=None):
    """The descriptor for the bench this session targets.

    Selected by $HIL_BENCH. When that is unset and exactly one bench is
    described, that one is used -- a single-bench fleet should not need
    configuring. With several described, the choice has to be explicit: picking
    alphabetically would silently run somebody's tests on the wrong hardware.

    Raises rather than exiting, so test fixtures can skip on it.
    """
    benches = load_descriptors()
    bench_id = bench_id or os.environ.get("HIL_BENCH")
    if bench_id:
        if bench_id not in benches:
            raise KeyError(f"unknown bench '{bench_id}'; known: "
                           f"{', '.join(sorted(benches)) or '(none)'}")
        return benches[bench_id][1]
    if len(benches) == 1:
        return next(iter(benches.values()))[1]
    raise RuntimeError(
        "several benches are described; set $HIL_BENCH to one of: "
        + ", ".join(sorted(benches)))


def slot_for_dut(desc, dut):
    """Which MLC slot holds `dut` on this bench."""
    for slot, spec in sorted((desc.get("slots") or {}).items()):
        if spec.get("dut") == dut:
            return int(slot)
    raise KeyError(f"{desc['id']} declares no slot with dut: {dut}")


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
              "dac": [], "dac_devid": {}, "dac_bad_devid": [], "nrf24": False,
              "can": {}, "slots_powered": None, "warnings": []}

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
        result["dac_devid"][str(idx)] = f"0x{dev_id:04X}"
        if dev_id != DAC_DEVID_EXPECTED:
            result["dac_bad_devid"].append(idx)

    if result["dac_bad_devid"]:
        got = ", ".join(f"DAC{i}={result['dac_devid'][str(i)]}"
                        for i in result["dac_bad_devid"])
        result["warnings"].append(
            f"DAC(s) {result['dac_bad_devid']} did not return the expected device "
            f"id 0x{DAC_DEVID_EXPECTED:04X} ({got}) — the SPI read is not landing. "
            "Check PWR_OK gating (invariant #6), then try a full PSU off/on cycle "
            "plus `systemctl restart hil-broker`. Any capability driven by a DAC "
            "(stim-pack-current) is untrustworthy until this clears.")

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
# host setup checks (doctor)
# --------------------------------------------------------------------------
#
# `verify` answers "does the hardware match what this bench declares?".
# `doctor` answers the question a NEW bench owner has: "did I build this Pi
# correctly?" -- i.e. does the host match docs/getting-started.md. Both are
# needed: a perfectly-described bench with an unpatched kernel module still
# cannot talk to a carrier.
#
# Section numbers match the headings in docs/getting-started.md so a failure
# points at the step to redo.

APT_PACKAGES = ["python3-can", "can-utils", "device-tree-compiler", "xz-utils",
                "libudev-dev", "pkg-config", "git", "curl"]
REQUIRED_GROUPS = ["spi", "i2c", "gpio", "dialout", "netdev"]
BOOT_CONFIG = "/boot/firmware/config.txt"
CONFIG_LINES = ["dtoverlay=mcp2515-triple", "gpio=7=op,dl", "gpio=8=ip,pd"]
UNITS = ["hil-psu-on", "hil-can-up", "hil-broker", "hil-dashboard"]


def _sh(cmd):
    """(rc, output). Shell-out is the point here -- these are host facts that
    only the system can answer."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=20)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:
        return 1, str(exc)


def doctor_checks():
    """Yield (section, name, ok, detail) for every documented setup step."""
    rc, out = _sh("dpkg -s " + " ".join(APT_PACKAGES) + " >/dev/null 2>&1")
    yield ("2", "apt packages", rc == 0,
           "all present" if rc == 0 else "one or more missing; see §2")

    _, groups = _sh("id -nG")
    missing = [g for g in REQUIRED_GROUPS if g not in groups.split()]
    yield ("1", "user groups", not missing,
           "ok" if not missing else f"missing: {' '.join(missing)}")

    rc, _ = _sh("test -f /boot/firmware/overlays/mcp2515-triple.dtbo")
    yield ("4", "device-tree overlay", rc == 0,
           "installed" if rc == 0 else "mcp2515-triple.dtbo not in /boot/firmware/overlays")

    _, cfg = _sh(f"cat {BOOT_CONFIG} 2>/dev/null")
    absent = [l for l in CONFIG_LINES if l not in cfg]
    yield ("4", "config.txt entries", not absent,
           "ok" if not absent else f"missing: {', '.join(absent)}")

    # The patched module cannot be identified by grepping the binary for
    # "backplane_hil": the patch's markers are C comments, which the compiler
    # strips, so that check fails on a correctly built bench. Compare against
    # the stock module the build script preserves instead.
    kver = os.uname().release
    mod = f"/lib/modules/{kver}/kernel/drivers/net/can/spi/mcp251x.ko.xz"
    rc_a, a = _sh(f"md5sum {mod} 2>/dev/null | cut -d' ' -f1")
    rc_b, b = _sh(f"md5sum {mod}.orig 2>/dev/null | cut -d' ' -f1")
    if rc_b != 0 or not b:
        yield ("5", "patched mcp251x", False,
               "no mcp251x.ko.xz.orig — the patched module was never built here")
    else:
        yield ("5", "patched mcp251x", a != b,
               "differs from stock" if a != b else "identical to stock: patch not installed")

    _, dm = _sh("dmesg 2>/dev/null | grep -c 'MCP2515 successfully initialized'")
    ok = dm.isdigit() and int(dm) >= 3
    yield ("5", "all three MCP2515 bound", ok, f"{dm or 0}/3 initialised")

    # Not `test -r`: the drop-in is 0440 root:root, so the bench user cannot
    # read it even when it is correctly installed. Ask sudo what it grants
    # instead, which also proves the escalation actually works rather than that
    # a file happens to exist.
    _, sudo_l = _sh("sudo -n -l 2>/dev/null")
    yield ("6", "sudoers drop-in", "ip link set can" in sudo_l,
           "NOPASSWD ip link set canN granted" if "ip link set can" in sudo_l
           else "no ip-link escalation for this user")

    for unit in UNITS:
        _, en = _sh(f"systemctl is-enabled {unit} 2>/dev/null")
        _, ac = _sh(f"systemctl is-active {unit} 2>/dev/null")
        yield ("7", f"{unit}", en == "enabled" and ac == "active",
               f"enabled={en or '-'} active={ac or '-'}")

    for dev in ("/dev/spidev0.3", "/dev/i2c-1"):
        rc, _ = _sh(f"test -e {dev}")
        yield ("8", dev, rc == 0, "present" if rc == 0 else "missing")

    # `pinctrl get 7 8` (space-separated, as the docs had it) errors with
    # "Too many arguments" on current pinctrl. Commas are the accepted form.
    # Parse per line: pinctrl indents inconsistently and the first line comes
    # back without its leading space, so substring matching on " 7:" is brittle.
    _, pins = _sh("pinctrl get 7,8 2>/dev/null")
    lines = {}
    for line in pins.splitlines():
        line = line.strip()
        if ":" in line:
            lines[line.split(":", 1)[0].strip()] = line
    g7, g8 = lines.get("7", ""), lines.get("8", "")
    yield ("8", "GPIO7 PS_ON# low", "op" in g7 and "lo" in g7,
           g7 or "GPIO7 not reported")
    yield ("8", "GPIO8 PWR_OK high", "ip" in g8 and "hi" in g8,
           g8 or "GPIO8 not reported")

    rc, _ = _sh("test -S /run/hil-broker/broker.sock")
    yield ("8", "broker socket", rc == 0, "present" if rc == 0 else "missing")

    _, code = _sh("curl -s -o /dev/null -w '%{http_code}' "
                  "http://localhost:8080/api/status")
    yield ("9", "dashboard :8080", code == "200", f"HTTP {code or '---'}")

    rc, ver = _sh("can-flasher --version 2>/dev/null")
    yield ("10", "can-flasher", rc == 0, ver or "not on PATH")


SUITES_FILE = REPO_ROOT / "configs" / "suites.yaml"


def load_suites():
    """Named pytest targets per DUT, from configs/suites.yaml."""
    if not SUITES_FILE.is_file():
        return {}
    import yaml
    return yaml.safe_load(SUITES_FILE.read_text()) or {}


def resolve_suite(dut, spec):
    """Expand a suite SPEC into pytest targets.

    A developer should be able to say what to run without memorising paths, and
    without being handed the whole tree. So:

      ""            -> the DUT's `smoke` suite (must be green on a good bench)
      "dv"          -> that DUT's named suite
      "tests/..."   -> passed through untouched, so an arbitrary path or a
                       single `file::Class::case` still works

    Anything containing "/" or "::" is treated as a path. An unknown NAME is an
    error rather than a silent fall-through to the whole tree -- a typo that
    quietly ran 63 cases instead of 20 is exactly the surprise this removes.
    """
    spec = (spec or "").strip()
    suites = load_suites().get(dut, {})

    if spec and ("/" in spec or "::" in spec):
        return spec.split()

    name = spec or "smoke"
    if name not in suites:
        known = ", ".join(sorted(suites)) or "(none defined)"
        raise SystemExit(
            f"unknown suite '{name}' for dut '{dut}'. Known: {known}\n"
            f"  or give a path, e.g. tests/hil/{dut}/test_block_a_boot.py")
    return list(suites[name])


BENCH_LOCK = "/tmp/hil-bench.lock"


def _sh(cmd, quiet=False):
    """Run a shell command, returning True on success. Recovery is best-effort
    at every rung: a step that cannot run must not stop the ladder."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if not quiet:
        print(f"    $ {cmd}" + ("" if r.returncode == 0 else f"   -> rc={r.returncode}"))
    return r.returncode == 0


def _psu_cycle(off_s=5.0, on_s=6.0):
    """POR the rails. This is what actually un-wedges a DAC80504 -- a broker
    restart alone re-opens the SPI handle without resetting the chip."""
    try:
        from broker.server import BrokerClient
        c = BrokerClient(os.environ.get("HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))
    except Exception as e:
        print(f"    cannot reach the broker to cycle the PSU: {e}")
        return False
    try:
        print("    PSU off"); c.call("psu.power", on=False); time.sleep(off_s)
        print("    PSU on");  c.call("psu.power", on=True);  time.sleep(on_s)
        return True
    except Exception as e:
        print(f"    PSU cycle failed: {e}")
        return False


def recover(level):
    """One rung of the recovery ladder. Cheapest first.

    1  restart the broker. Sometimes enough on its own.
    2  POR the rails, then rebuild everything that a POR knocks over.

    Level 2 reloads mcp251x deliberately: a PSU cycle resets the MCP2515s while
    the kernel still believes the interfaces are up, so CAN goes silent with the
    link still reading UP/ERROR-ACTIVE. That looks exactly like a dead DUT and
    has been misdiagnosed as a brick.
    """
    if level <= 1:
        print("  recovery 1: restarting the broker")
        _sh("sudo systemctl restart hil-broker")
        time.sleep(6)
        return

    print("  recovery 2: PSU power-on-reset, then CAN + broker")
    _psu_cycle()
    _sh("sudo modprobe -r mcp251x"); time.sleep(2)
    _sh("sudo modprobe mcp251x");    time.sleep(3)
    _sh("sudo systemctl restart hil-can-up"); time.sleep(3)
    _sh("sudo systemctl restart hil-broker"); time.sleep(6)


def cmd_recover(args):
    """Bring a wedged bench back, under the bench lock.

    The lock is not optional. Level 2 power-cycles the rails, and doing that
    while another run is mid-flash is precisely the interrupted-write that
    leaves an H7 unrecoverable (F-077). Taking it here rather than in the
    caller means a hand-run recovery is protected too.
    """
    import fcntl
    with open(BENCH_LOCK, "w") as lk:
        print(f"  waiting for the bench lock ({BENCH_LOCK})")
        fcntl.flock(lk, fcntl.LOCK_EX)
        print("  lock held; nothing else can be flashing")
        recover(args.level)
    return 0


def cmd_doctor(args):
    """Check this host against the documented bench build."""
    if not sys.platform.startswith("linux"):
        sys.exit("doctor inspects the bench host — run it on the Pi")

    failures = 0
    section = None
    for sec, name, ok, detail in doctor_checks():
        if sec != section:
            print(f"\n§{sec}  {'-' * 52}")
            section = sec
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<26} {detail}")
        failures += 0 if ok else 1

    print()
    if failures:
        print(f"{failures} check(s) failed — see the matching section of "
              "docs/getting-started.md")
    else:
        print("this bench matches the documented build")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_suite(args):
    for t in resolve_suite(args.dut, args.suite):
        print(t)


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
    required = {c.strip() for c in (args.capabilities or "").split(",") if c.strip()}
    benches = load_descriptors()

    # An explicit bench always wins. Capability matching is the good default for
    # throughput, but someone standing at a bench with a scope on a pin needs the
    # run pinned to that bench, not to whichever one happens to be free.
    if args.bench:
        if args.bench not in benches:
            print(f"unknown bench '{args.bench}'; known: "
                  f"{', '.join(sorted(benches)) or '(none)'}", file=sys.stderr)
            return 1
        desc = benches[args.bench][1]
        missing = required - set(desc.get("capabilities", []))
        if missing:
            print(f"{args.bench} was requested explicitly but lacks: "
                  f"{' '.join(sorted(missing))}", file=sys.stderr)
            return 1
        matches = [(args.bench, desc)]
    else:
        if not required:
            print("give --capabilities or --bench", file=sys.stderr)
            return 1
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

    # Capability-level honesty: routing trusts `capabilities`, so a declared
    # capability whose hardware does not answer is worse than an absent one --
    # it silently attracts runs this bench cannot serve.
    caps = desc.get("capabilities", [])
    # A bad DAC only invalidates what rides on THAT DAC. This used to fire on
    # any bad DAC and always blame stim-pack-current -- so when DACs 0 and 1
    # wedged it named pack current (idx 3, healthy) and never mentioned that
    # idx 0, the ECU's brake and APPS driver, was dead. Wrong in both
    # directions, and the reader concludes "an AMS thing, not my problem".
    bad_dacs = set(found["dac_bad_devid"])
    if bad_dacs:
        routing = desc.get("routing", {})
        users = {}
        for rname, spec in routing.items():
            if isinstance(spec, dict) and "dac" in spec:
                users.setdefault(int(spec["dac"]), set()).add(rname)
        for idx in sorted(bad_dacs):
            who = ", ".join(sorted(users.get(idx, ()))) or "nothing in `routing` names it"
            problems.append(
                f"DAC {idx} does not return a valid device id (drives: {who})")
        pack_dacs = {int(spec["dac"]) for rname, spec in routing.items()
                     if rname in ("pack_current", "current_heartbeat")
                     and isinstance(spec, dict) and "dac" in spec}
        if "stim-pack-current" in caps and (pack_dacs & bad_dacs):
            problems.append(
                "stim-pack-current is declared, but the DAC it routes through "
                f"({sorted(pack_dacs & bad_dacs)}) is not answering")
    if "radio-nrf24" in caps and not found["nrf24"]:
        problems.append("radio-nrf24 is declared, but no nRF24 responds")

    # A declared sample point that the live bus contradicts is the single
    # highest-value check here: it is invisible until a DUT mysteriously bus-offs.
    for role, spec in desc.get("can", {}).items():
        dev = spec.get("dev")
        live = found["can"].get(dev)
        if not live:
            problems.append(f"can[{role}]: {dev} not present on this host")
            continue
        if "bitrate" in spec and "bitrate" in live and spec["bitrate"] != live["bitrate"]:
            problems.append(f"can[{role}] {dev}: declared bitrate={spec['bitrate']}, "
                            f"live bitrate={live['bitrate']}")
        # `ip` prints the sample point to three decimals (0.6875 renders as
        # 0.687) and the kernel quantises to the nearest achievable bit timing,
        # so an exact compare calls a correctly configured bus broken. The
        # tolerance is still an order of magnitude tighter than the mistake this
        # check exists to catch (0.875 against 0.6875).
        if "sample_point" in spec and "sample_point" in live:
            if abs(spec["sample_point"] - live["sample_point"]) > SAMPLE_POINT_TOL:
                problems.append(
                    f"can[{role}] {dev}: declared sample_point={spec['sample_point']}, "
                    f"live sample_point={live['sample_point']}")

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
    p.add_argument("--capabilities",
                   help="comma-separated, e.g. dut-ams,stim-temps")
    p.add_argument("--bench",
                   help="pin to this bench; still checked against --capabilities")
    p.add_argument("--github-output", action="store_true",
                   help="also append bench/labels to $GITHUB_OUTPUT")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("suite", help="expand a suite name (or path) to pytest targets")
    p.add_argument("--dut", required=True)
    p.add_argument("--suite", default="", help="name (smoke/dv/full/...) or a path")
    p.set_defaults(func=cmd_suite)

    p = sub.add_parser("recover",
                       help="unwedge a bench that fails its own preflight")
    p.add_argument("--bench")
    p.add_argument("--level", type=int, default=2,
                   help="1 = broker restart, 2 = PSU power-on-reset + CAN + broker")
    p.set_defaults(func=cmd_recover)

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

    sub.add_parser("doctor",
                   help="check this host against the documented bench build"
                   ).set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
