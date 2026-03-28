#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-ifs08hil}"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || realpath "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)")"

# Use TTY only if stdout is a terminal (local dev).
TTY_OPTS=()
if [ -t 1 ]; then
  TTY_OPTS=(-it)
fi

docker run --rm "${TTY_OPTS[@]}" \
  -v "${ROOT}:/workspace" -w /workspace \
  --device /dev/bus/usb:/dev/bus/usb --privileged \
  "$IMAGE" \
  bash -lc '
set -euo pipefail

# Use the firmware'\''s own toolchain file if present (e.g. CubeMX-generated),
# otherwise fall back to the HIL default toolchain.
if [ -f /workspace/firmware/cmake/gcc-arm-none-eabi.cmake ]; then
  TOOLCHAIN=/workspace/firmware/cmake/gcc-arm-none-eabi.cmake
  echo "Using firmware toolchain: $TOOLCHAIN"
else
  TOOLCHAIN=/workspace/docker/toolchain-arm-none-eabi.cmake
  echo "Using HIL default toolchain: $TOOLCHAIN"
fi

# Ensure our pinned ARM toolchain is found first regardless of which cmake file is used
export PATH="/opt/toolchains/arm-gcc/bin:$PATH"

cmake -S firmware -B build -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN"
cmake --build build -j
'