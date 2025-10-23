# Reproducible STM32 build+flash image (works on Pi 4/5)
FROM debian:bookworm-slim

ARG ARM_GCC_VER=12.3.rel1
ARG ARM_GCC_URL=https://developer.arm.com/-/media/Files/downloads/gnu/12.3.rel1/binrel
ARG ARM_GCC_TAR=gcc-arm-12.3.rel1-x86_64-arm-none-eabi.tar.xz

# Base tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils git cmake ninja-build make \
    python3 python3-pip python3-venv \
    openocd dfu-util usbutils udev \
    && rm -rf /var/lib/apt/lists/*

# ARM GCC (official release)
RUN mkdir -p /opt/toolchains && \
    curl -fsSL ${ARM_GCC_URL}/${ARM_GCC_TAR} -o /tmp/gcc.tar.xz && \
    tar -xf /tmp/gcc.tar.xz -C /opt/toolchains && rm /tmp/gcc.tar.xz && \
    ln -s /opt/toolchains/gcc-arm-12.3.rel1-x86_64-arm-none-eabi /opt/toolchains/arm-gcc

ENV PATH="/opt/toolchains/arm-gcc/bin:${PATH}"

# Python deps for HIL test harness
RUN pip3 install --no-cache-dir pytest pytest-xdist pytest-html pyserial python-can

# Workdir + entrypoint
WORKDIR /workspace
COPY passthrough.sh /usr/local/bin/passthrough.sh
RUN chmod +x /usr/local/bin/passthrough.sh
ENTRYPOINT ["/usr/local/bin/passthrough.sh"]