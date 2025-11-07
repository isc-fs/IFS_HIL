FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG ARM_GCC_VER=12.3.rel1
ARG TARGETARCH

# Mapear TARGETARCH de buildx -> nombre de tarball de Arm GNU Toolchain
RUN set -eux; case "$TARGETARCH" in \
    arm64) echo aarch64> /tmp/host_arch ;; \
    amd64) echo x86_64> /tmp/host_arch ;; \
    *) echo "Arquitectura no soportada: $TARGETARCH" >&2; exit 1 ;; \
  esac

# Paquetes 
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils git cmake ninja-build make \
    python3 python3-venv python3-dev python3-pip \
    build-essential pkg-config \
    libusb-1.0-0 libusb-1.0-0-dev libffi-dev libssl-dev \
    openocd dfu-util usbutils udev \
 && rm -rf /var/lib/apt/lists/*

# Toolchain ARM GCC
ARG ARM_GCC_URL="https://armkeil.blob.core.windows.net/developer/Files/downloads/gnu/${ARM_GCC_VER}/binrel"
RUN set -eux; HOST_ARCH=$(cat /tmp/host_arch); \
  TARBALL="arm-gnu-toolchain-${ARM_GCC_VER}-${HOST_ARCH}-arm-none-eabi.tar.xz"; \
  echo "Downloading ${ARM_GCC_URL}/${TARBALL}"; \
  curl -fL --retry 5 --retry-delay 2 "${ARM_GCC_URL}/${TARBALL}" -o /tmp/gcc.tar.xz; \
  mkdir -p /opt/toolchains; \
  tar -xf /tmp/gcc.tar.xz -C /opt/toolchains; \
  rm /tmp/gcc.tar.xz; \
  ln -s /opt/toolchains/arm-gnu-toolchain-${ARM_GCC_VER}-${HOST_ARCH}-arm-none-eabi /opt/toolchains/arm-gcc
ENV PATH="/opt/toolchains/arm-gcc/bin:${PATH}"

# Python en venv para evitar PEP 668 (entorno externally-managed)
RUN python3 -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
      'pytest>=8,<9' 'pytest-xdist>=3,<4' 'pytest-html>=4,<5' \
      'pyserial>=3,<4' 'python-can>=4,<5'

WORKDIR /workspace

COPY docker/passthrough.sh /usr/local/bin/passthrough.sh

RUN chmod +x /usr/local/bin/passthrough.sh

ENTRYPOINT ["/usr/local/bin/passthrough.sh"]