#!/usr/bin/env bash
# Install the isc-fs/can-flasher Rust binary on the Pi.
#
# Fast path: downloads the prebuilt aarch64-unknown-linux-gnu tarball
# from the pinned release. Falls back to `cargo install` from source
# only if the release asset can't be fetched (e.g. we're behind a
# proxy, or a future tag hasn't published the aarch64 binary yet).
#
# The prebuilt binary is ~1 MB and extracts to /usr/local/bin in a
# few seconds; the cargo fallback needs rustup + ~5 minutes of CPU on
# a Pi 4.
set -euo pipefail

CAN_FLASHER_TAG="v1.1.2"
TARGET="aarch64-unknown-linux-gnu"
REPO="isc-fs/can-flasher"
ASSET="can-flasher-${CAN_FLASHER_TAG}-${TARGET}.tar.gz"
RELEASE_URL="https://github.com/${REPO}/releases/download/${CAN_FLASHER_TAG}/${ASSET}"
INSTALL_DIR="/usr/local/bin"

echo "--- system deps ---"
sudo apt-get install -y libudev-dev pkg-config

fetch_prebuilt() {
  local tmp
  tmp="$(mktemp -d)"
  trap "rm -rf '$tmp'" RETURN
  echo "--- downloading ${ASSET} ---"
  if ! curl -fsSL --retry 2 -o "${tmp}/${ASSET}" "${RELEASE_URL}"; then
    return 1
  fi
  tar -xzf "${tmp}/${ASSET}" -C "${tmp}"
  local stage="${tmp}/can-flasher-${CAN_FLASHER_TAG}-${TARGET}"
  [ -x "${stage}/can-flasher" ] || return 1
  sudo install -m 0755 "${stage}/can-flasher" "${INSTALL_DIR}/can-flasher"
  return 0
}

build_from_source() {
  echo "--- no prebuilt binary available, falling back to cargo install ---"
  sudo apt-get install -y build-essential
  if ! command -v cargo >/dev/null 2>&1; then
    echo "--- installing rustup (one-time) ---"
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --default-toolchain stable
    # shellcheck source=/dev/null
    . "$HOME/.cargo/env"
  fi
  cargo install --git "https://github.com/${REPO}" --tag "${CAN_FLASHER_TAG}" --locked
  # cargo install drops the binary in ~/.cargo/bin; symlink to /usr/local/bin
  # for a consistent install location across both paths.
  sudo ln -sf "${HOME}/.cargo/bin/can-flasher" "${INSTALL_DIR}/can-flasher"
}

if ! fetch_prebuilt; then
  build_from_source
fi

echo "--- verify ---"
can-flasher --version
can-flasher adapters
