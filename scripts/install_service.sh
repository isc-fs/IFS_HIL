#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[install_service] Installing systemd unit..."
sudo cp "${PROJECT_ROOT}/infra/systemd/hil-agent.service" /etc/systemd/system/hil-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now hil-agent.service

echo "[install_service] Installing udev rules..."
sudo cp "${PROJECT_ROOT}/infra/udev/99-hil.rules" /etc/udev/rules.d/99-hil.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
