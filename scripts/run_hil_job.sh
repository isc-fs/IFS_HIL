#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${PROJECT_ROOT}/build/firmware"
LOG_DIR="${PROJECT_ROOT}/logs"
FIRMWARE_BIN="${BUILD_DIR}/hil_firmware.bin"

mkdir -p "$BUILD_DIR" "$LOG_DIR"

LOG_FILE="${LOG_DIR}/hil-$(date +%Y%m%d-%H%M%S).log"

{
    echo "[hil-job] Starting HIL job..."

    if command -v cmake >/dev/null 2>&1; then
        echo "[hil-job] Configuring firmware build..."
        cmake -S "${PROJECT_ROOT}/firmware" -B "$BUILD_DIR"
        echo "[hil-job] Building firmware..."
        cmake --build "$BUILD_DIR"
    else
        echo "[hil-job] cmake not found; skipping firmware build."
    fi

    if [ -f "$FIRMWARE_BIN" ]; then
        echo "[hil-job] Flashing firmware..."
        if command -v hil >/dev/null 2>&1; then
            hil flash --firmware "$FIRMWARE_BIN" --target stm32f103
        else
            python3 -m tools.flash flash --firmware "$FIRMWARE_BIN" --target stm32f103
        fi
    else
        echo "[hil-job] Firmware binary missing; flash step skipped."
    fi

    if command -v pytest >/dev/null 2>&1; then
        echo "[hil-job] Running tests..."
        pytest "${PROJECT_ROOT}/tests"
    else
        echo "[hil-job] pytest not available; skipping tests."
    fi

    echo "[hil-job] Job complete."
} | tee "$LOG_FILE"
