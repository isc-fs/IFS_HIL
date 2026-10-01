#!/usr/bin/env bash
# Flash pico_ltc_emulator UF2 to a connected Pi Pico without pressing
# BOOTSEL.
#
# Strategy:
#   1. If the Pico is currently in CDC mode, send the `BSL\n` command
#      so it re-enumerates as USB MSC (RP2040 bootloader).
#   2. Run picotool to load the UF2 and execute it.
#
# Required: picotool installed (`sudo apt install picotool` on Pi /
# `brew install picotool` on Mac).

set -euo pipefail

UF2="${1:-}"

if [[ -z "${UF2}" || ! -f "${UF2}" ]]; then
    echo "usage: $0 <pico_ltc_emulator.uf2>" >&2
    exit 1
fi

if ! command -v picotool >/dev/null 2>&1; then
    echo "picotool not found. Install:" >&2
    echo "  apt:  sudo apt install picotool" >&2
    echo "  brew: brew install picotool" >&2
    exit 2
fi

# Resolve a connected CDC port that looks like a Pico, if any. Best
# effort -- if the script can't find one, picotool's `reboot -f -u`
# will still try to do the right thing via picoboot.
PORT=""
for cand in /dev/ttyACM* /dev/cu.usbmodem*; do
    [[ -e "${cand}" ]] || continue
    # `udevadm` only on Linux; on macOS we skip the check and just
    # take the first match.
    if command -v udevadm >/dev/null 2>&1; then
        if udevadm info -q property "${cand}" 2>/dev/null \
                | grep -q 'ID_VENDOR_ID=2e8a'; then
            PORT="${cand}"; break
        fi
    else
        PORT="${cand}"; break
    fi
done

if [[ -n "${PORT}" ]]; then
    echo "[1/2] sending BSL via ${PORT} ..."
    # Open the port and send the command. If the port doesn't actually
    # exist or doesn't respond, we fall through to picotool reboot -f.
    printf 'BSL\n' > "${PORT}" || true
    sleep 1.5
else
    echo "[1/2] no Pico CDC port found; using picotool reboot -f -u"
    picotool reboot -f -u 2>/dev/null || true
    sleep 1.5
fi

echo "[2/2] loading ${UF2} ..."
picotool load -fx "${UF2}"
echo "Done. Pico should be running new firmware."
