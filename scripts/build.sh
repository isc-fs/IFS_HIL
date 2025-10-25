#!/usr/bin/env bash
set -euo pipefail
IMAGE="${IMAGE:-ifs08hil}"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || realpath "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)")"

docker run --rm -it \
  -v "${ROOT}:/workspace" -w /workspace \
  --device /dev/bus/usb:/dev/bus/usb --privileged \
  "$IMAGE" \
  bash -lc '
cmake -S firmware -B build -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE=/workspace/docker/toolchain-arm-none-eabi.cmake && \
    cmake --build build -j
  ' 