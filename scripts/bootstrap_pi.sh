#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=${1:-/srv/hil}

echo "[bootstrap] Installing base dependencies..."
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip cmake ninja-build can-utils openocd

if [ ! -d "$REPO_DIR" ]; then
    echo "[bootstrap] Preparing workspace at $REPO_DIR"
    sudo mkdir -p "$REPO_DIR"
    sudo chown "$(id -u)":"$(id -g)" "$REPO_DIR"
fi

if [ ! -d "$REPO_DIR/.git" ]; then
    echo "[bootstrap] Clone your firmware repository into $REPO_DIR"
    echo "           e.g. git clone <your-remote-url> $REPO_DIR"
else
    echo "[bootstrap] Repository already present; skipping clone."
fi
