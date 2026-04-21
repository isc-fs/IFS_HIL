#!/usr/bin/env bash
# Build and install a patched mcp251x.ko for the BACKPLANE_HIL PCB.
#
# Fetches the upstream raspberrypi/linux mcp251x.c, applies our patch,
# builds as an out-of-tree module against the running kernel's headers,
# compresses as .ko.xz, and installs over the stock module. A backup of
# the stock module is kept at ${TARGET}.orig the first time this runs.
#
# Requires: linux-headers-$(uname -r), curl, xz, make, gcc.
# Run on the Pi, not on a workstation.
set -euo pipefail

SRC_URL="https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.12.y/drivers/net/can/spi/mcp251x.c"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${HERE}/_build"
TARGET="/lib/modules/$(uname -r)/kernel/drivers/net/can/spi/mcp251x.ko.xz"

rm -rf "$WORK"
mkdir -p "$WORK"
cp "$HERE/Makefile" "$WORK/"
curl -fsSL -o "$WORK/mcp251x.c" "$SRC_URL"

echo "--- applying patch ---"
patch -d "$WORK" -p1 < "$HERE/0001-backplane-hil-spi-quirks.patch"

echo "--- building ---"
make -C "$WORK"

echo "--- installing (sudo required) ---"
sudo cp -n "$TARGET" "${TARGET}.orig" 2>/dev/null || true
sudo xz -z -f "$WORK/mcp251x.ko"
sudo cp "$WORK/mcp251x.ko.xz" "$TARGET"
sudo depmod -a

echo "--- done. Backup at ${TARGET}.orig ---"
echo "    Reboot (or modprobe -r mcp251x && modprobe mcp251x) to pick up the new module."
