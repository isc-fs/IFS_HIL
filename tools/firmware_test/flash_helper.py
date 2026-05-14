"""
`can-flasher` subprocess wrapper. Standalone Python because the Rust binary is
the canonical flasher and shelling out is the right pattern for the bench.

Example:

    from tools.firmware_test.flash_helper import CanFlasher

    fl = CanFlasher(channel="can2", node_id=0x01)
    fl.discover()                          # returns parsed node list or []
    fl.flash("/tmp/AMS.bin", verify=True, jump=True)
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


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
    """Wraps the `can-flasher` CLI for SocketCAN."""

    def __init__(self, channel: str = "can2", bitrate: int = 500_000,
                 node_id: int = 0x01, timeout_ms: int = 5000,
                 binary: str = "can-flasher"):
        self.channel    = channel
        self.bitrate    = bitrate
        self.node_id    = node_id
        self.timeout_ms = timeout_ms
        self.binary     = binary

    # -- low-level argv builders ---------------------------------------

    def _base_args(self) -> List[str]:
        return [self.binary,
                "--interface", "socketcan",
                "--channel", self.channel,
                "--bitrate", str(self.bitrate)]

    def _node_args(self) -> List[str]:
        return ["--node-id", f"0x{self.node_id:X}",
                "--timeout", str(self.timeout_ms)]

    # -- commands -------------------------------------------------------

    def discover(self, timeout_ms: Optional[int] = None) -> List[DiscoveredNode]:
        """Run `can-flasher discover` and parse the output table.
        Returns an empty list if no bootloaders replied."""
        args = self._base_args() + ["discover",
                                    "--timeout-ms", str(timeout_ms or self.timeout_ms)]
        r = subprocess.run(args, capture_output=True, text=True, timeout=15)
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
        """Run `can-flasher flash <image>`. Raises CalledProcessError on
        non-zero exit; returns the CompletedProcess otherwise."""
        args = (self._base_args() + self._node_args() +
                ["flash", str(image_path),
                 "--address", f"0x{address:X}"])
        if verify: args.append("--verify-after")
        if jump:   args.append("--jump")
        if extra_args: args.extend(extra_args)
        log.info("can-flasher flash %s", " ".join(args[1:]))
        return subprocess.run(args, capture_output=True, text=True,
                              check=True, timeout=timeout_s)

    def send_boot_trigger(self) -> None:
        """Shortcut: cansend the boot-trigger frame on this CAN channel.
        Useful when an app is running and we want to drop it back to BL."""
        subprocess.run(["cansend", self.channel, "002#B007AD11"],
                       check=True, capture_output=True)

    def app_to_bl(self) -> None:
        """Older path some apps support: send-raw 0x001 03 06 01.
        Use this when the app implements the `APP_CTRL ENTER_BOOTLOADER`
        opcode rather than parsing the boot trigger directly."""
        args = self._base_args() + self._node_args() + [
            "send-raw", "0x001", "03", "06", "01"]
        subprocess.run(args, check=True, capture_output=True, timeout=5)
