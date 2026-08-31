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
  -e GIT_HASH="${GIT_HASH:-}" \
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

# Stamp firmware build provenance (AMS #323) when the caller passed
# GIT_HASH=$(git -C <ams-repo> rev-parse --short=8 HEAD). firmware/ carries
# no .git, so the hash must come from the caller. Also write firmware/GIT_HASH
# so the HIL A-013 row can assert 0x6C6[3..6] on the non-git Pi.
GIT_HASH_ARG=()
if [ -n "${GIT_HASH:-}" ]; then
  GIT_HASH_ARG=(-DGIT_HASH="$GIT_HASH")
  echo "$GIT_HASH" > /workspace/firmware/GIT_HASH
  echo "Stamping git hash: $GIT_HASH"
fi

cmake -S firmware -B build -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" "${GIT_HASH_ARG[@]}"
cmake --build build -j

# Ensure .bin and .hex exist — some firmware CMakeLists.txt only produce .elf
find /workspace/build -maxdepth 1 -name "*.elf" | while read -r elf; do
  echo "Converting $elf -> .hex / .bin"
  arm-none-eabi-objcopy -O ihex   "$elf" "${elf%.elf}.hex"
  arm-none-eabi-objcopy -O binary "$elf" "${elf%.elf}.bin"
done
'