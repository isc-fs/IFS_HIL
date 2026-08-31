#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# NOT A HIL BENCH SCRIPT. Despite living in scripts/, this belongs to the
# accu-charger project: it installs Docker and builds/runs the `accu-charger`
# image. Nothing in this repository invokes it.
#
# Running it on a BACKPLANE_HIL bench actively breaks the bench:
#   - adds `dtparam=spi=on`, a second claimant on SPI0 alongside our overlay
#   - adds `dtoverlay=mcp2515-can0` (ONE controller, cs_pin 22 / irq 25) which
#     conflicts with `mcp2515-triple` (THREE controllers on GPIO27/17/18,
#     irq 4/5/6) -- they fight over the same bus
#   - brings canN up with no sample-point and no txqueuelen, i.e. the 0.875
#     mismatch that bus-offs every DUT and the qlen-10 ENOBUFS during flashing
#
# To bring a HIL bench up, you do not need a script: systemd does it at boot
# (hil-psu-on -> hil-can-up -> hil-broker -> hil-dashboard). See
# docs/quickstart.md.
#
# The guard below refuses to run on a host configured as a HIL bench. Remove
# this file once the charger project has its own home.
# ---------------------------------------------------------------------------
for _cfg in /boot/firmware/config.txt /boot/config.txt; do
    if [ -f "$_cfg" ] && grep -qE '^\s*dtoverlay=mcp2515-triple' "$_cfg"; then
        echo "!! Refusing to run: $_cfg has dtoverlay=mcp2515-triple, so this" >&2
        echo "   host is a BACKPLANE_HIL bench. This script would add a" >&2
        echo "   conflicting overlay and reset the CAN bit timing." >&2
        echo "   To start the bench: sudo systemctl start hil-psu-on hil-can-up \\" >&2
        echo "                              hil-broker hil-dashboard" >&2
        exit 1
    fi
done


# ==============================
# CONFIGURACIÓN DEL PROYECTO
# ==============================
CAN_IFACE="${CAN_IFACE:-can0}"
CAN_BITRATE="${CAN_BITRATE:-500000}"
MCP_OSC_HZ="${MCP_OSC_HZ:-16000000}"
MCP_CS_GPIO="${MCP_CS_GPIO:-22}"
MCP_IRQ_GPIO="${MCP_IRQ_GPIO:-25}"
APP_PORT="${APP_PORT:-1111}"
IMAGE_NAME="${IMAGE_NAME:-accu-charger}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
DOCKERFILE_PATH="docker/Dockerfile"
PROJECT_ROOT="$(realpath "$(dirname "$0")/..")"

# ==============================
# FUNCIONES AUXILIARES
# ==============================
need_reboot_flag=0

detect_boot_cfg_path() {
  if [[ -f /boot/firmware/config.txt ]]; then
    echo "/boot/firmware/config.txt"
  else
    echo "/boot/config.txt"
  fi
}

ensure_line_in_file() {
  local line="$1" file="$2"
  grep -qxF "$line" "$file" 2>/dev/null || echo "$line" | tee -a "$file" >/dev/null
}

enable_spi_and_mcp2515() {
  local cfg
  cfg="$(detect_boot_cfg_path)"
  echo "[i] Usando configuración en: $cfg"

  # Habilitar SPI
  if ! grep -qE '^\s*dtparam=spi=on' "$cfg"; then
    echo "[i] Activando SPI"
    ensure_line_in_file "dtparam=spi=on" "$cfg"
    need_reboot_flag=1
  fi

  # Overlay MCP2515
  local overlay="dtoverlay=mcp2515-can0,oscillator=${MCP_OSC_HZ},interrupt=${MCP_IRQ_GPIO},cs_pin=${MCP_CS_GPIO}"
  if ! grep -qE "^\s*dtoverlay=mcp2515-can0" "$cfg"; then
    echo "[i] Añadiendo overlay MCP2515: $overlay"
    ensure_line_in_file "$overlay" "$cfg"
    need_reboot_flag=1
  fi

  # Módulos CAN persistentes
  mkdir -p /etc/modules-load.d
  cat >/etc/modules-load.d/can.conf <<EOF
can
can_raw
can_dev
mcp251x
EOF
}

install_docker_if_needed() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[i] Instalando Docker..."
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      tee /etc/apt/sources.list.d/docker.list >/dev/null
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
  else
    echo "[i] Docker ya está instalado."
  fi
}

create_can0_systemd() {
  cat >/etc/systemd/system/can0.service <<EOF
[Unit]
Description=Bring up SocketCAN ${CAN_IFACE}
After=network-pre.target spi-config.service
Wants=network-pre.target
ConditionPathExists=/sys/class/net/${CAN_IFACE}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip link set ${CAN_IFACE} down || true
ExecStart=/sbin/ip link set ${CAN_IFACE} type can bitrate ${CAN_BITRATE}
ExecStart=/sbin/ip link set ${CAN_IFACE} up
ExecStop=/sbin/ip link set ${CAN_IFACE} down

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable can0.service || true
}

bring_up_can_now() {
  if ip link show "$CAN_IFACE" >/dev/null 2>&1; then
    echo "[i] Configurando ${CAN_IFACE} a ${CAN_BITRATE} bit/s"
    ip link set "$CAN_IFACE" down || true
    ip link set "$CAN_IFACE" type can bitrate "$CAN_BITRATE"
    ip link set "$CAN_IFACE" up
    ip -details link show "$CAN_IFACE" || true
  else
    echo "[!] ${CAN_IFACE} todavía no existe. Tras reiniciar aparecerá."
  fi
}

build_image() {
  echo "[i] Construyendo imagen Docker ${IMAGE_NAME}:${IMAGE_TAG}"
  cd "$PROJECT_ROOT"
  docker buildx create --use --name rpi-builder >/dev/null 2>&1 || true
  docker buildx build \
    --platform linux/arm/v7,linux/arm64 \
    -t "${IMAGE_NAME}:${IMAGE_TAG}" \
    -f "$DOCKERFILE_PATH" \
    --load \
    .
}

run_container() {
  echo "[i] Lanzando contenedor Docker"
  cd "$PROJECT_ROOT"
  docker rm -f "${IMAGE_NAME}" >/dev/null 2>&1 || true
  docker run -d \
    --name "${IMAGE_NAME}" \
    --restart unless-stopped \
    --network host \
    -e CAN_IFACE="${CAN_IFACE}" \
    -e APP_PORT="${APP_PORT}" \
    "${IMAGE_NAME}:${IMAGE_TAG}"
}

# ==============================
# EJECUCIÓN PRINCIPAL
# ==============================
if [[ $EUID -ne 0 ]]; then
  echo "⚠️  Este script debe ejecutarse como root: sudo bash scripts/setup_and_run.sh"
  exit 1
fi

enable_spi_and_mcp2515
install_docker_if_needed
create_can0_systemd

if [[ $need_reboot_flag -eq 1 ]]; then
  echo
  echo "⚠️  Se han aplicado cambios en /boot/config.txt o overlays."
  echo "🔁  Es necesario REINICIAR la Raspberry Pi."
  echo "Ejecuta: sudo reboot"
  echo "Después del reinicio, vuelve a lanzar este script."
  exit 0
fi

bring_up_can_now
build_image
run_container

echo
echo "✅ Proyecto desplegado correctamente."
echo "   - Interfaz CAN: ${CAN_IFACE} @ ${CAN_BITRATE} bit/s"
echo "   - Contenedor: ${IMAGE_NAME}"
echo "   - Panel web: http://<IP-de-la-raspberry>:${APP_PORT}"