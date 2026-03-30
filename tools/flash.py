#!/usr/bin/env python3
"""
tools/flash.py — Non-interactive CAN bootloader flash tool for the IFS08 HIL bench.

Entry points (pyproject.toml):
    hil flash <firmware.bin>          # CLI
    flash_tool: tools.flash:flash     # programmatic (from YAML config)

Protocol summary (FDCAN2 @ 500 kbps, classic CAN frames):
    Host → Bootloader : CAN ID 0x21
    Bootloader → Host : CAN ID 0x22
    Host → Application: CAN ID 0x31  (remote enter-bootloader)
    Application → Host: CAN ID 0x32
"""

from __future__ import annotations

import logging
import os
import sys
import time
import zlib
from typing import Optional

import can
import typer

log = logging.getLogger(__name__)

# ── Protocol constants ────────────────────────────────────────────────────────

CAN_BOOT_RX_ID = 0x21  # Host → Bootloader
CAN_BOOT_TX_ID = 0x22  # Bootloader → Host

APP_CTRL_RX_ID = 0x31  # Host → Application
APP_CTRL_TX_ID = 0x32  # Application → Host

BL_CMD_PING           = 0x01
BL_CMD_JUMP_TO_APP    = 0x02
BL_CMD_ERASE_APP      = 0x10
BL_CMD_WRITE_DATA     = 0x20
BL_CMD_WRITE_FINISH   = 0x21
BL_CMD_SET_SIZE       = 0x30
BL_CMD_SET_CRC        = 0x31
BL_CMD_SET_VERSION    = 0x32
BL_CMD_GET_INFO       = 0x40

APP_CMD_ENTER_BOOTLOADER = 0x01

_GET_INFO_STATUS: dict[int, str] = {
    0x00: "Valid application present",
    0x10: "Incorrect magic",
    0x11: "Invalid size",
    0x12: "Invalid CRC value",
    0x13: "Metadata address mismatch",
    0x14: "CRC does not match stored CRC",
    0x15: "Stack pointer out of range",
    0x16: "Entry point out of range",
}

_WRITE_FINISH_STATUS: dict[int, str] = {
    0x00: "OK — firmware written and CRC validated",
    0x01: "CRC mismatch",
    0x02: "Metadata write error",
    0x03: "Missing size/CRC/version configuration",
    0xFE: "Flash programming error on last FLASHWORD",
}

# ── Low-level helpers ─────────────────────────────────────────────────────────


def _open_bus(channel: str, bitrate: int) -> can.Bus:
    log.info("Opening CAN bus: channel=%s  bitrate=%d bps", channel, bitrate)
    return can.Bus(interface="socketcan", channel=channel, bitrate=bitrate)


def _send_boot(bus: can.Bus, data: list[int]) -> None:
    """Send an 8-byte frame to the bootloader RX ID."""
    msg = can.Message(
        arbitration_id=CAN_BOOT_RX_ID,
        is_extended_id=False,
        data=bytes(data),
    )
    bus.send(msg)


def _wait_ack(bus: can.Bus, expected_cmd: int, timeout: float = 1.0) -> Optional[int]:
    """
    Wait for a bootloader ACK frame (ID=CAN_BOOT_TX_ID, data[0]==expected_cmd).
    Returns the status byte (data[1]) or None on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        msg = bus.recv(timeout=max(remaining, 0.0))
        if msg is None:
            break
        if msg.arbitration_id != CAN_BOOT_TX_ID:
            continue
        if len(msg.data) < 2:
            continue
        if msg.data[0] == expected_cmd:
            return msg.data[1]
    return None


def _simple_cmd(bus: can.Bus, cmd: int, timeout: float = 1.0) -> Optional[int]:
    """Send a 1-byte command and return its ACK status, or None on timeout."""
    _send_boot(bus, [cmd] + [0] * 7)
    status = _wait_ack(bus, cmd, timeout=timeout)
    if status is None:
        log.error("CMD 0x%02X — no ACK (timeout after %.1f s)", cmd, timeout)
    else:
        log.debug("CMD 0x%02X — ACK status=0x%02X", cmd, status)
    return status


# ── Bootloader command wrappers ───────────────────────────────────────────────


def bl_ping(bus: can.Bus, timeout: float = 0.5) -> bool:
    """Return True if the bootloader responds to PING."""
    _send_boot(bus, [BL_CMD_PING] + [0] * 7)
    return _wait_ack(bus, BL_CMD_PING, timeout=timeout) is not None


def bl_get_info(bus: can.Bus) -> tuple[Optional[int], int, int]:
    """
    Query bootloader for current application status.

    Returns:
        (status, size_bytes, version_u16)
        status == 0x00 means a valid application is present.
        status == None means no response (timeout).
    """
    _send_boot(bus, [BL_CMD_GET_INFO] + [0] * 7)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        rx = bus.recv(timeout=max(deadline - time.monotonic(), 0.0))
        if rx is None:
            break
        if rx.arbitration_id != CAN_BOOT_TX_ID:
            continue
        if len(rx.data) < 2 or rx.data[0] != BL_CMD_GET_INFO:
            continue
        status = rx.data[1]
        size = version = 0
        if status == 0x00 and len(rx.data) >= 6:
            size = int.from_bytes(rx.data[2:6], "little")
            if len(rx.data) >= 8:
                version = int.from_bytes(rx.data[6:8], "little")
        return status, size, version
    log.error("GET_INFO — no response (timeout)")
    return None, 0, 0


def bl_erase(bus: can.Bus) -> bool:
    """Erase the application flash area (sectors 1–7). Returns True on success."""
    log.info("Erasing application area (sectors 1–7)...")
    status = _simple_cmd(bus, BL_CMD_ERASE_APP, timeout=30.0)
    if status == 0x00:
        log.info("Erase OK")
        return True
    log.error(
        "Erase failed: status=%s",
        f"0x{status:02X}" if status is not None else "timeout",
    )
    return False


def bl_set_metadata(bus: can.Bus, fw: bytes, version: int) -> bool:
    """
    Send SET_SIZE, SET_CRC and SET_VERSION to the bootloader.
    CRC32 is calculated using the standard IEEE 802.3 algorithm (zlib.crc32).
    """
    fw_len = len(fw)
    crc32 = zlib.crc32(fw) & 0xFFFFFFFF
    log.info(
        "Firmware metadata — size=%d bytes  CRC32=0x%08X  version=0x%04X (%d.%d)",
        fw_len, crc32, version, (version >> 8) & 0xFF, version & 0xFF,
    )

    for cmd, value in (
        (BL_CMD_SET_SIZE,    fw_len),
        (BL_CMD_SET_CRC,     crc32),
        (BL_CMD_SET_VERSION, version),
    ):
        payload = [cmd] + list(value.to_bytes(4, "little")) + [0, 0, 0]
        _send_boot(bus, payload)
        st = _wait_ack(bus, cmd, timeout=1.0)
        if st is None or st != 0x00:
            log.error(
                "CMD 0x%02X failed: status=%s",
                cmd,
                f"0x{st:02X}" if st is not None else "timeout",
            )
            return False
    return True


def bl_write_data(bus: can.Bus, fw: bytes) -> bool:
    """
    Stream the firmware image to the bootloader using WRITE_DATA frames.
    Each frame carries 7 bytes of payload; the last frame is padded with 0xFF.
    """
    fw_len = len(fw)
    total_frames = (fw_len + 6) // 7
    log.info("Writing firmware: %d bytes across %d CAN frames...", fw_len, total_frames)

    offset = 0
    frame_idx = 0

    while offset < fw_len:
        chunk = fw[offset : offset + 7]
        if len(chunk) < 7:
            chunk = chunk + b"\xFF" * (7 - len(chunk))

        _send_boot(bus, [BL_CMD_WRITE_DATA] + list(chunk))

        status = _wait_ack(bus, BL_CMD_WRITE_DATA, timeout=1.0)
        if status is None:
            log.error("WRITE_DATA — no ACK at offset %d / %d", offset, fw_len)
            return False
        if status != 0x00:
            log.error(
                "WRITE_DATA — error 0x%02X at offset %d / %d", status, offset, fw_len
            )
            return False

        offset += 7
        frame_idx += 1

        if frame_idx % 200 == 0 or offset >= fw_len:
            actual = min(offset, fw_len)
            log.info("  Progress: %d / %d bytes (%.1f%%)", actual, fw_len, 100.0 * actual / fw_len)

    log.info("Data write complete.")
    return True


def bl_write_finish(bus: can.Bus) -> bool:
    """Flush the flash buffer, validate CRC and write metadata block."""
    log.info("Sending WRITE_FINISH (flush + CRC verify)...")
    status = _simple_cmd(bus, BL_CMD_WRITE_FINISH, timeout=30.0)
    desc = _WRITE_FINISH_STATUS.get(
        status,  # type: ignore[arg-type]
        f"unknown 0x{status:02X}" if status is not None else "timeout",
    )
    if status == 0x00:
        log.info("WRITE_FINISH OK: %s", desc)
        return True
    log.error("WRITE_FINISH failed: %s", desc)
    return False


def bl_jump(bus: can.Bus) -> bool:
    """Request the bootloader to jump to the application."""
    log.info("Sending JUMP_TO_APP...")
    status = _simple_cmd(bus, BL_CMD_JUMP_TO_APP, timeout=2.0)
    # The board may reset before the ACK arrives — treat None as non-fatal.
    if status is None:
        log.warning("JUMP_TO_APP — no ACK (board may have already reset)")
        return True
    if status == 0x00:
        log.info("Jump OK")
        return True
    desc = _GET_INFO_STATUS.get(status, f"0x{status:02X}")
    log.error("JUMP_TO_APP rejected by bootloader: %s", desc)
    return False


def bl_remote_enter(bus: can.Bus, boot_timeout: float = 3.0) -> bool:
    """
    Ask the running application to reboot into bootloader via CAN (ID 0x31),
    then poll PING until the bootloader responds.
    """
    log.info("Requesting application to enter bootloader (ID=0x%02X)...", APP_CTRL_RX_ID)
    msg = can.Message(
        arbitration_id=APP_CTRL_RX_ID,
        is_extended_id=False,
        data=bytes([APP_CMD_ENTER_BOOTLOADER] + [0] * 7),
    )
    bus.send(msg)

    # Best-effort ACK from application (it may reset before replying)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        rx = bus.recv(timeout=max(deadline - time.monotonic(), 0.0))
        if rx is None:
            break
        if (
            rx.arbitration_id == APP_CTRL_TX_ID
            and len(rx.data) >= 2
            and rx.data[0] == APP_CMD_ENTER_BOOTLOADER
        ):
            log.debug("Application ACK: 0x%02X", rx.data[1])
            break

    log.info("Waiting for bootloader to come up (timeout=%.1f s)...", boot_timeout)
    deadline = time.monotonic() + boot_timeout
    while time.monotonic() < deadline:
        if bl_ping(bus, timeout=0.15):
            log.info("Bootloader is up.")
            return True
        time.sleep(0.05)

    log.error("Bootloader did not respond within %.1f s", boot_timeout)
    return False


# ── High-level flash entry point ──────────────────────────────────────────────


def flash(
    firmware_path: str,
    channel: str = "can0",
    bitrate: int = 500_000,
    version: int = 0x0100,
    force: bool = False,
) -> bool:
    """
    Flash a firmware binary over the CAN bootloader protocol.

    This is the programmatic entry point used by the HIL agent
    (referenced as ``flash_tool: tools.flash:flash`` in ECU YAML configs).

    Args:
        firmware_path: Absolute or relative path to the ``.bin`` firmware file.
        channel:       SocketCAN interface name (e.g. ``"can0"``, ``"can1"``).
        bitrate:       CAN bus bitrate in bps. Must match the bootloader config (500 000).
        version:       Firmware version encoded as ``(major << 8) | minor``.
        force:         Re-flash even when the installed version matches ``version``.

    Returns:
        ``True`` on success, ``False`` on any failure.
    """
    if not os.path.isfile(firmware_path):
        log.error("Firmware file not found: %s", firmware_path)
        return False

    with open(firmware_path, "rb") as fh:
        fw = fh.read()

    if not fw:
        log.error("Firmware file is empty: %s", firmware_path)
        return False

    log.info("Loaded firmware: %s (%d bytes)", firmware_path, len(fw))

    bus = _open_bus(channel, bitrate)
    try:
        # ── Step 1: ensure the bootloader is running ──────────────────────────
        log.info("Checking for bootloader presence...")
        if not bl_ping(bus, timeout=0.5):
            log.info("Bootloader not detected — requesting application to reboot into bootloader...")
            if not bl_remote_enter(bus):
                log.error("Could not reach bootloader. Aborting.")
                return False
        else:
            log.info("Bootloader already active.")

        # ── Step 2: skip re-flash if same version is already installed ────────
        if not force:
            status, cur_size, cur_ver = bl_get_info(bus)
            if status == 0x00 and cur_ver != 0 and cur_ver == version:
                log.warning(
                    "Version 0x%04X (%d.%d) is already installed (%d bytes). "
                    "Skipping flash. Use --force to override.",
                    cur_ver,
                    (cur_ver >> 8) & 0xFF,
                    cur_ver & 0xFF,
                    cur_size,
                )
                return True  # Already up-to-date — treat as success

        # ── Step 3: erase application area ───────────────────────────────────
        if not bl_erase(bus):
            return False

        # ── Step 4: configure size, CRC32 and version ────────────────────────
        if not bl_set_metadata(bus, fw, version):
            return False

        # ── Step 5: stream firmware data ──────────────────────────────────────
        if not bl_write_data(bus, fw):
            return False

        # ── Step 6: flush buffer and verify CRC ──────────────────────────────
        if not bl_write_finish(bus):
            return False

        # ── Step 7: post-flash verification ──────────────────────────────────
        status, size, ver = bl_get_info(bus)
        if status != 0x00:
            desc = _GET_INFO_STATUS.get(status, f"0x{status:02X}" if status is not None else "timeout")
            log.error("Post-flash GET_INFO validation failed: %s", desc)
            return False
        log.info(
            "Post-flash verification OK — size=%d bytes  version=0x%04X (%d.%d)",
            size, ver, (ver >> 8) & 0xFF, ver & 0xFF,
        )

        # ── Step 8: jump to application ───────────────────────────────────────
        if not bl_jump(bus):
            return False

        log.info("Flash complete.")
        return True

    finally:
        bus.shutdown()


# ── CLI ───────────────────────────────────────────────────────────────────────

app = typer.Typer(
    name="hil-flash",
    help="Flash STM32 firmware over the IFS08 CAN bootloader.",
    add_completion=False,
)


@app.command()
def main(
    firmware: str = typer.Argument(..., help="Path to .bin firmware file"),
    channel: str = typer.Option("can0", "--channel", "-c", help="SocketCAN interface (e.g. can0, can1)"),
    bitrate: int = typer.Option(500_000, "--bitrate", "-b", help="CAN bitrate in bps"),
    version_major: int = typer.Option(1, "--major", help="Firmware major version (0–255)"),
    version_minor: int = typer.Option(0, "--minor", help="Firmware minor version (0–255)"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-flash even if same version is installed"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug-level logging"),
) -> None:
    """Flash STM32 firmware over the IFS08 CAN bootloader."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if not 0 <= version_major <= 255 or not 0 <= version_minor <= 255:
        log.error("Version values must be in range 0–255.")
        raise typer.Exit(1)

    version = (version_major << 8) | version_minor
    ok = flash(
        firmware_path=firmware,
        channel=channel,
        bitrate=bitrate,
        version=version,
        force=force,
    )
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
