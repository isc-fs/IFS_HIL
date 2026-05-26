#!/usr/bin/env python3
"""Reflash the AMS-running carrier (default MLC2) via the BL-trigger
frame, with a power-cycle fallback.

In the car the trigger is the *only* practical way to reflash: the
AMS enclosure has no accessible reset switch and battery disconnect
requires opening the accumulator. The AMS firmware listens for the
trigger on FDCAN1 (accumulator bus) regardless of FSM state -- Start,
Precharge, Run, Charge, Error -- per IFS08-CE-AMS
Bootloader::matches_trigger semantics.

Trigger contract (mirror in any pit-tool / can-flasher wrapper):
  - bus     : FDCAN1 (= Pi-side can0, PCB CAN3)
  - id      : 0x002 (11-bit standard)
  - dlc     : 4
  - payload : B0 07 AD 11   (== ams::config::BlBootReqPayload)

What the firmware does on receipt: opens all relays, drains TX FIFO,
writes BL_BOOT_REQ_MAGIC (0xB00710AD) into RTC->BKP0R, then
NVIC_SystemReset. On reset, the BL sees the magic and stays in BL
mode (skips auto-jump) -- so flash succeeds on the first try, no
N-attempt power-cycle race.

Fallback path (power-cycle via TCA9555) only fires if the chip is
totally unresponsive to the trigger -- expected when the firmware
itself is bricked.
"""

import argparse
import os
import subprocess
import sys
import time

TRIGGER_CHANNEL = "can0"
TRIGGER_FRAME   = "002#B007AD11"
FLASH_CHANNEL   = "can2"
NODE_ID         = "0x1"
APP_ADDRESS     = "0x08020000"
BITRATE         = "500000"

# MLC slot wiring on BACKPLANE_HIL: K1..K4 = TCA 0x20 port0 pins 0..3.
DEFAULT_TCA_ADDR = 0x20
DEFAULT_TCA_PORT = 0


def send_trigger(channel: str = TRIGGER_CHANNEL) -> None:
    subprocess.run(["cansend", channel, TRIGGER_FRAME],
                   check=True, timeout=2)


def discover_bl(channel: str = FLASH_CHANNEL,
                timeout_ms: int = 3000) -> bool:
    """True iff at least one node responded on the flash bus. The
    'Node' header text is always printed; only a row whose first
    non-whitespace token is a hex literal (0xNN) means a real BL
    answered."""
    r = subprocess.run(
        ["can-flasher",
         "--interface", "socketcan", "--channel", channel,
         "--bitrate", BITRATE, "--node-id", NODE_ID,
         "--timeout", str(timeout_ms), "discover"],
        capture_output=True, text=True,
        timeout=int(timeout_ms / 1000) + 5)
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        if line.lstrip().lower().startswith("0x"):
            return True
    return False


def flash(bin_path: str, channel: str = FLASH_CHANNEL,
          jump: bool = True, timeout_ms: int = 15000):
    cmd = ["can-flasher",
           "--interface", "socketcan", "--channel", channel,
           "--bitrate", BITRATE, "--node-id", NODE_ID,
           "--timeout", str(timeout_ms),
           "flash", bin_path, "--address", APP_ADDRESS,
           "--no-verify-after"]
    if jump:
        cmd.append("--jump")
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=180)


def power_cycle(slot: int = 2,
                tca_addr: int = DEFAULT_TCA_ADDR,
                tca_port: int = DEFAULT_TCA_PORT,
                off_dwell_s: float = 3.0,
                on_settle_s: float = 0.5) -> None:
    # Broker client lives in the IFS08_HIL checkout on the bench Pi.
    # Try the in-repo path first (script run from `tools/`), then the
    # canonical bench install at /home/isc/IFS08_HIL so the helper
    # also works when copied to /tmp.
    candidates = [
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "/home/isc/IFS08_HIL",
    ]
    for p in candidates:
        if p not in sys.path:
            sys.path.insert(0, p)
    from broker.server import BrokerClient
    pin = slot - 1
    c = BrokerClient(os.environ.get(
        "HIL_BROKER_SOCKET", "/run/hil-broker/broker.sock"))
    c.call("tca.write_pin", addr=tca_addr, port=tca_port,
           pin=pin, value=False)
    time.sleep(off_dwell_s)
    c.call("tca.write_pin", addr=tca_addr, port=tca_port,
           pin=pin, value=True)
    time.sleep(on_settle_s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bin", help="path to AMS.bin (raw .bin, not .elf)")
    ap.add_argument("--slot", type=int, default=2,
                    help="MLC slot 1..4 (default 2); only used by fallback")
    ap.add_argument("--no-jump", action="store_true",
                    help="leave the chip in BL after flash (skip --jump)")
    ap.add_argument("--no-fallback", action="store_true",
                    help="if the trigger path fails, give up; "
                         "do not power-cycle")
    ap.add_argument("--max-fallback-attempts", type=int, default=10,
                    help="how many power-cycle attempts in fallback "
                         "(default 10)")
    args = ap.parse_args()

    if not os.path.isfile(args.bin):
        print(f"!! binary not found: {args.bin}", file=sys.stderr)
        return 2

    # ---- Path 0: skip trigger if BL is already there ----
    # The chip might already be in BL mode -- previous --no-jump flash,
    # interrupted boot, or a previous trigger reboot that's still parked
    # (BKP0R magic keeps the BL waiting). Send-trigger would do nothing
    # because no app is listening.
    print("==> probing if BL is already up", flush=True)
    if discover_bl():
        print("   BL already up -- flashing directly", flush=True)
        r = flash(args.bin, jump=not args.no_jump)
        if r.returncode == 0:
            tail = r.stdout.strip().splitlines()[-1] \
                if r.stdout.strip() else "(no stdout)"
            print(f"   OK -- {tail}")
            return 0
        err_tail = r.stderr.strip().splitlines()[-1] \
            if r.stderr.strip() else r.stdout[-160:]
        print(f"   direct flash failed: {err_tail}", flush=True)

    # ---- Path 1: trigger ----
    print("==> trigger path: cansend can0 002#B007AD11", flush=True)
    try:
        send_trigger()
    except Exception as e:
        print(f"   trigger send failed: {e}", flush=True)
    else:
        # The trigger + relay-open + TX-drain + system_reset takes a
        # few hundred ms; BL then opens its FIFO. 1 s is generous.
        time.sleep(1.0)
        if discover_bl():
            print("   BL responding -- flashing", flush=True)
            r = flash(args.bin, jump=not args.no_jump)
            if r.returncode == 0:
                tail = r.stdout.strip().splitlines()[-1] \
                    if r.stdout.strip() else "(no stdout)"
                print(f"   OK -- {tail}")
                return 0
            err_tail = r.stderr.strip().splitlines()[-1] \
                if r.stderr.strip() else r.stdout[-160:]
            print(f"   flash failed after trigger: {err_tail}",
                  flush=True)
        else:
            print("   BL did not respond after trigger -- chip is "
                  "either bricked or FDCAN1 RX is wedged", flush=True)

    if args.no_fallback:
        print("==> --no-fallback set, giving up", flush=True)
        return 1

    # ---- Path 2: power-cycle fallback ----
    print(f"==> power-cycle fallback (slot {args.slot}, up to "
          f"{args.max_fallback_attempts} attempts)", flush=True)
    for attempt in range(1, args.max_fallback_attempts + 1):
        try:
            power_cycle(slot=args.slot)
        except Exception as e:
            print(f"   attempt {attempt}: power-cycle failed: {e}",
                  flush=True)
            continue
        if not discover_bl():
            print(f"   attempt {attempt}: missed BL window",
                  flush=True)
            continue
        r = flash(args.bin, jump=not args.no_jump)
        if r.returncode == 0:
            tail = r.stdout.strip().splitlines()[-1] \
                if r.stdout.strip() else "(no stdout)"
            print(f"   attempt {attempt}: OK -- {tail}")
            return 0
        err_tail = r.stderr.strip().splitlines()[-1] \
            if r.stderr.strip() else "unknown"
        print(f"   attempt {attempt}: FAIL -- {err_tail}",
              flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
