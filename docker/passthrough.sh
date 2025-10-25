#!/usr/bin/env bash
set -euo pipefail

# Ensure udev devices are visible inside the container (for ST-Link, DFU, CAN, etc.)
if [ -d /run/udev ]; then
    mountpoint -q /run/udev || mount -t devtmpfs none /run/udev 2>/dev/null || true
fi

# Execute any command passed to the container
exec "$@"