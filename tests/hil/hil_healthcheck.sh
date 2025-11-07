#!/usr/bin/env bash
set -euo pipefail

CAN_IFACE="${CAN_IFACE:-can0}"
BITRATE="${CAN_BITRATE:-500000}"
OSC="${MCP_OSC_HZ:-16000000}"   # 8000000 si tu placa es de 8 MHz
SPIBUS="${MCP_SPI_BUS:-spi0-0}" # spi0-0 (CE0) o spi0-1 (CE1)
INTGPIO="${MCP_IRQ_GPIO:-25}"

PASS=()
FAIL=()
NOTE=()

section(){ echo -e "\n===== $* ====="; }

ok(){ echo "✅ $1"; PASS+=("$1"); }
ko(){ echo "❌ $1"; FAIL+=("$1"); }
nt(){ echo "ℹ️  $1"; NOTE+=("$1"); }

# 0) Requisitos básicos
section "Sistema"
uname -a || true
if vcgencmd get_throttled &>/dev/null; then
  THR=$(vcgencmd get_throttled || true)
  echo "throttled: $THR"
  [[ "$THR" =~ 0x0$ ]] && ok "Sin throttling/undervoltage" || nt "Posible undervoltage: $THR"
else
  nt "vcgencmd no disponible (ok en algunos OS)"
fi

# 1) SPI habilitado y nodos spidev
section "SPI"
if [ -f /boot/firmware/config.txt ]; then CFG=/boot/firmware/config.txt; else CFG=/boot/config.txt; fi
echo "CFG=$CFG"
grep -nE '^(dtparam=spi=on|dtoverlay=mcp2515.*)' "$CFG" || true
ls -l /dev/spidev* || true
grep -q '^dtparam=spi=on' "$CFG" && ok "SPI habilitado en config" || ko "SPI no está habilitado"
[ -e /dev/spidev0.0 ] || [ -e /dev/spidev0.1 ] && ok "Nodos /dev/spidev presentes" || ko "No hay /dev/spidev*"

# 2) Overlay mcp2515 cargado (runtime) y módulos
section "Driver CAN"
lsmod | egrep '(^can|^mcp251x)' || true
dmesg | grep -i -E 'mcp2515|mcp251x|spi|can' | tail -n 80 || true

ip -brief link show type can || true
if ip link show type can | grep -q "$CAN_IFACE"; then
  ok "Interfaz $CAN_IFACE detectada"
else
  nt "Intento cargar overlay en caliente: mcp2515,$SPIBUS,oscillator=$OSC,interrupt=$INTGPIO"
  sudo dtoverlay -r 0 2>/dev/null || true
  if sudo dtoverlay -v mcp2515 "$SPIBUS" "oscillator=$OSC" "interrupt=$INTGPIO"; then
    sleep 1
  fi
  ip link show type can | grep -q "$CAN_IFACE" && ok "Interfaz $CAN_IFACE creada tras dtoverlay" || ko "No aparece $CAN_IFACE (revisar CS/INT/osc)"
fi

# 3) Subir can0 y hacer smoke loopback
section "Smoke CAN (loopback)"
if ip link show "$CAN_IFACE" &>/dev/null; then
  sudo ip link set "$CAN_IFACE" down 2>/dev/null || true
  sudo ip link set "$CAN_IFACE" type can bitrate "$BITRATE" loopback on
  sudo ip link set "$CAN_IFACE" up
  if command -v candump >/dev/null && command -v cansend >/dev/null; then
    (candump "$CAN_IFACE" & C=$!; sleep 0.2; cansend "$CAN_IFACE" 123#DEADBEEF; sleep 0.6; kill $C || true) && ok "Loopback OK (candump/cansend)"
  else
    ko "Faltan can-utils (candump/cansend). Instala: sudo apt-get install -y can-utils"
  fi
  ip -s -d link show "$CAN_IFACE" | sed -n '1,40p'
else
  nt "Saltando loopback: $CAN_IFACE no existe"
fi

# 4) Systemd (servicio can0)
section "systemd can0.service"
if systemctl cat can0.service &>/dev/null; then
  systemctl is-enabled can0.service &>/dev/null && ok "can0.service habilitado" || nt "can0.service no habilitado"
  systemctl --no-pager --full status can0.service || true
else
  nt "No hay can0.service (opcional)"
fi

# 5) Docker (si lo usas)
section "Docker"
if command -v docker &>/dev/null; then
  docker --version
  groups | grep -q docker && ok "Usuario en grupo docker" || nt "Usuario no está en grupo docker"
  nt "Si tu contenedor usa CAN, arráncalo con --cap-add NET_RAW,NET_ADMIN y --network host"
else
  nt "Docker no instalado (ok si no lo necesitas)"
fi

# 6) Resumen
section "Resumen"
echo "OKs:   ${#PASS[@]}"
printf '  - %s\n' "${PASS[@]}" 2>/dev/null || true
echo "NOTAS: ${#NOTE[@]}"
printf '  - %s\n' "${NOTE[@]}" 2>/dev/null || true
echo "FALLOS:${#FAIL[@]}"
printf '  - %s\n' "${FAIL[@]}" 2>/dev/null || true

[ "${#FAIL[@]}" -eq 0 ] && { echo -e "\n✅ HEALTHCHECK PASS"; exit 0; } || { echo -e "\n❌ HEALTHCHECK FAIL"; exit 1; }
