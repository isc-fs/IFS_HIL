"""
`can-flasher` subprocess wrapper. Standalone Python because the Rust binary is
the canonical flasher and shelling out is the right pattern for the bench.

Example:

    from tools.firmware_test.flash_helper import CanFlasher, CanFlasherError

    fl = CanFlasher(channel="can2", node_id=0x01)
    fl.discover()                          # returns parsed node list or []
    try:
        fl.flash("/tmp/AMS.bin", verify=True, jump=True)
    except CanFlasherError as e:
        # e.stdout / e.stderr carry the actual flasher output — surfaced
        # in str(e) too, so a bare `raise` in a test gives a useful trace.
        ...
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


class CanFlasherError(RuntimeError):
    """`can-flasher` subprocess returned a non-zero exit.

    Unlike a bare `subprocess.CalledProcessError`, this class includes the
    captured stdout / stderr in `str(self)` so pytest's traceback (and any
    `raise` re-throw) shows the real reason — NO_VALID_APP, FLASH_HW,
    TRANSPORT_TIMEOUT, etc. — instead of just "exit status 1".
    """

    def __init__(self, *, cmd: List[str], returncode: int,
                 stdout: str, stderr: str):
        self.cmd_argv  = list(cmd)
        self.returncode = returncode
        self.stdout    = stdout or ""
        self.stderr    = stderr or ""

        def _trim(s: str, n: int = 800) -> str:
            s = (s or "").rstrip()
            if not s:
                return "(empty)"
            return s if len(s) <= n else s[:n] + " …(truncated)"

        super().__init__(
            "can-flasher exit {rc} on `{cmd}`\n"
            "  stdout: {out}\n"
            "  stderr: {err}".format(
                rc=returncode,
                cmd=shlex.join(self.cmd_argv),
                out=_trim(self.stdout),
                err=_trim(self.stderr),
            )
        )


def _run(cmd: List[str], *, timeout_s: float,
         check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess with captured output; on non-zero exit (when
    `check=True`) raise CanFlasherError with stdout/stderr inline."""
    log.info("$ %s", " ".join(cmd[1:]))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if check and r.returncode != 0:
        raise CanFlasherError(cmd=cmd, returncode=r.returncode,
                              stdout=r.stdout, stderr=r.stderr)
    return r


@dataclass
class DiscoveredNode:
    node_id: int
    proto: str
    fw_version: str
    git_hash: str
    product: str
    wrp: bool
    reset_cause: str


class CanFlasher:
    """Wraps the `can-flasher` CLI for SocketCAN.

    Two timeouts, deliberately split:
      - `discover_timeout_ms` → `--timeout-ms` on the discover subcommand
        (how long to wait for any bootloader replies; should stay short
        because empty == fail-fast).
      - `per_frame_timeout_ms` → top-level `--timeout` (per-frame timeout
        for every ISO-TP transaction in flash / send-raw / etc.). Needs
        to be generous because the first write chunk after a fresh erase
        can take seconds while the BL settles. The `can-flasher` default
        of 500 ms is too aggressive; the IFS08 BL needs ≥ 10 s in
        practice.
    """

    def __init__(self, channel: str = "can2", bitrate: int = 500_000,
                 node_id: int = 0x01,
                 discover_timeout_ms: int = 3000,
                 per_frame_timeout_ms: int = 30_000,
                 binary: str = "can-flasher",
                 *,
                 timeout_ms: Optional[int] = None):
        """`timeout_ms=` (deprecated) maps to `discover_timeout_ms` for
        backwards compatibility with the v1 API."""
        if timeout_ms is not None:
            discover_timeout_ms = timeout_ms
        self.channel              = channel
        self.bitrate              = bitrate
        self.node_id              = node_id
        self.discover_timeout_ms  = discover_timeout_ms
        self.per_frame_timeout_ms = per_frame_timeout_ms
        self.binary               = binary

    # -- low-level argv builders ---------------------------------------

    def _base_args(self) -> List[str]:
        return [self.binary,
                "--interface", "socketcan",
                "--channel", self.channel,
                "--bitrate", str(self.bitrate)]

    def _node_args(self) -> List[str]:
        return ["--node-id", f"0x{self.node_id:X}",
                "--timeout", str(self.per_frame_timeout_ms)]

    # -- commands -------------------------------------------------------

    def discover(self, timeout_ms: Optional[int] = None) -> List[DiscoveredNode]:
        """Run `can-flasher discover` and parse the output table.
        Returns an empty list if no bootloaders replied. Other non-zero
        exits raise CanFlasherError with the captured output inline."""
        args = self._base_args() + ["discover",
                                    "--timeout-ms",
                                    str(timeout_ms or self.discover_timeout_ms)]
        # discover returns 0 on success including the "no bootloaders replied"
        # case (it's not an error to find nothing), so don't `check`.
        r = _run(args, timeout_s=15.0, check=False)
        if r.returncode != 0:
            raise CanFlasherError(cmd=args, returncode=r.returncode,
                                  stdout=r.stdout, stderr=r.stderr)
        if "No bootloaders replied" in (r.stdout + r.stderr):
            return []
        nodes: List[DiscoveredNode] = []
        for line in r.stdout.splitlines():
            m = re.match(r"^(0x[0-9A-Fa-f]+)\s+(\S+)\s+(.+?)\s{2,}(\S+)\s+(\S+)\s+(\S+)\s+(.+)$", line)
            if not m:
                continue
            nodes.append(DiscoveredNode(
                node_id=int(m.group(1), 16),
                proto=m.group(2),
                fw_version=m.group(3).strip(),
                git_hash=m.group(4),
                product=m.group(5),
                wrp=(m.group(6) == "✓"),
                reset_cause=m.group(7).strip(),
            ))
        return nodes

    def flash(self, image_path: str | Path, *,
              address: int = 0x08020000,
              verify: bool = True,
              jump: bool = True,
              extra_args: Optional[List[str]] = None,
              timeout_s: float = 60.0) -> subprocess.CompletedProcess:
        """Run `can-flasher flash <image>`. On non-zero exit raises
        CanFlasherError; the exception message includes the can-flasher
        stdout/stderr so the real reason (NO_VALID_APP, FLASH_HW, etc.)
        is visible in the pytest traceback."""
        args = (self._base_args() + self._node_args() +
                ["flash", str(image_path),
                 "--address", f"0x{address:X}"])
        if verify: args.append("--verify-after")
        if jump:   args.append("--jump")
        if extra_args: args.extend(extra_args)
        return _run(args, timeout_s=timeout_s)

    def send_boot_trigger(self, channel: Optional[str] = None) -> None:
        """Cansend the boot-trigger frame to drop a running app back to BL.

        `channel` overrides the default (this flasher's BL channel). Since
        the AMS v1.2.0-ltc6811 refactor (#73), the AMS app listens for
        the boot-trigger frame on FDCAN1 (the ACU bus) rather than FDCAN2
        (the BL bus). Callers from a HIL fixture should pass the ACU bus
        channel here. Falls back to the BL channel for older firmware /
        other ECUs."""
        ch = channel or self.channel
        _run(["cansend", ch, "002#B007AD11"], timeout_s=5.0)

    def app_to_bl(self) -> None:
        """Older path some apps support: send-raw 0x001 03 06 01.
        Use this when the app implements the `APP_CTRL ENTER_BOOTLOADER`
        opcode rather than parsing the boot trigger directly."""
        args = self._base_args() + self._node_args() + [
            "send-raw", "0x001", "03", "06", "01"]
        _run(args, timeout_s=5.0)
