#!/usr/bin/env bash
# Bring a fresh Raspberry Pi to a working HIL bench, unattended.
#
# Automates sections 1-7 of docs/getting-started.md -- the mechanical parts,
# ~50 of its 59 commands. What it deliberately does NOT do: burn a carrier's
# bootloader (SWD, out of band), install can-flasher (needs `gh` auth against a
# private repo), write a bench descriptor (only you know what is wired), or
# register a runner (needs a short-lived token). Those stay manual and are
# listed at the end of the run.
#
# Usage:
#   scripts/bench_bootstrap.sh --dry-run     # report what would change
#   scripts/bench_bootstrap.sh               # do it
#
# Idempotent: every step checks first, so re-running on a built bench changes
# nothing and reports "already done". Safe to re-run after a partial failure.
#
# Reboot required afterwards -- the overlay and the patched module only take
# effect on boot, which is why services are enabled but not started.

set -euo pipefail

DRY=0
case "${1:-}" in
    --dry-run|-n) DRY=1 ;;
    -h|--help)    sed -n '2,/^$/p' "$0" | sed -e 's/^# //' -e 's/^#$//'; exit 0 ;;
    "")           ;;
    *)            echo "unknown argument: $1" >&2; exit 2 ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOOT_CONFIG=/boot/firmware/config.txt
KVER="$(uname -r)"
MODULE="/lib/modules/$KVER/kernel/drivers/net/can/spi/mcp251x.ko.xz"
CHANGED=0

c_ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
c_do()   { printf '  \033[33mdo\033[0m    %s\n' "$*"; CHANGED=$((CHANGED+1)); }
c_skip() { printf '  --    %s\n' "$*"; }
step()   { printf '\n\033[1m%s\033[0m\n' "$*"; }

run() {
    if [ "$DRY" = 1 ]; then return 0; fi
    eval "$@"
}

# ---- guardrails ---------------------------------------------------------
# Refuse anywhere that is not a Pi bench: this edits boot config and installs
# kernel modules, and a wrong host is not a recoverable mistake.
[ -f "$BOOT_CONFIG" ] || {
    echo "!! $BOOT_CONFIG not found — this is not a Raspberry Pi OS host." >&2
    exit 1
}
[ "$(uname -m)" = "aarch64" ] || {
    echo "!! expected aarch64 (Pi OS 64-bit), got $(uname -m)." >&2
    exit 1
}
sudo -n true 2>/dev/null || {
    echo "!! passwordless sudo is required (this installs packages, a kernel" >&2
    echo "   module, systemd units and a sudoers drop-in)." >&2
    exit 1
}

[ "$DRY" = 1 ] && printf '\n*** DRY RUN — nothing will be changed ***\n'
printf '\nbench bootstrap — %s, kernel %s\n' "$(uname -n)" "$KVER"

# ---- 1. interfaces + groups --------------------------------------------
step "1. SPI/I2C interfaces and user groups"
# SPI deliberately not driven through raspi-config: the mcp2515-triple overlay
# provides SPI0 itself, and on a correctly built bench `get_spi` still reports
# "disabled" while /dev/spidev0.3 works fine. Adding dtparam=spi=on on top would
# put a second claimant on the same bus, so only enable it when neither the
# overlay nor the parameter is present.
if grep -qE '^dtoverlay=mcp2515-triple|^dtparam=spi=on' "$BOOT_CONFIG"; then
    c_ok "SPI provided by the mcp2515-triple overlay"
elif [ -e /dev/spidev0.0 ] || [ -e /dev/spidev0.3 ]; then
    c_ok "SPI functional (spidev present)"
else
    c_do "enable SPI"
    run "sudo raspi-config nonint do_spi 0"
fi
if [ "$(raspi-config nonint get_i2c 2>/dev/null)" = "0" ]; then
    c_ok "i2c already enabled"
else
    c_do "enable i2c"
    run "sudo raspi-config nonint do_i2c 0"
fi
MISSING_GROUPS=""
for g in spi i2c gpio dialout netdev; do
    id -nG | tr ' ' '\n' | grep -qx "$g" || MISSING_GROUPS="$MISSING_GROUPS,$g"
done
if [ -z "$MISSING_GROUPS" ]; then
    c_ok "user $(id -un) already in spi,i2c,gpio,dialout,netdev"
else
    c_do "add $(id -un) to${MISSING_GROUPS} (log out and back in afterwards)"
    run "sudo usermod -aG spi,i2c,gpio,dialout,netdev $(id -un)"
fi

# ---- 2. packages --------------------------------------------------------
step "2. System packages"
PKGS="python3-can can-utils device-tree-compiler xz-utils libudev-dev pkg-config git curl linux-headers-$KVER"
NEED=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || NEED="$NEED $p"; done
if [ -z "$NEED" ]; then
    c_ok "all present"
else
    c_do "apt-get install$NEED"
    run "sudo apt-get update -qq"
    run "sudo apt-get install -y -qq $NEED"
fi

# ---- 3. python package --------------------------------------------------
step "3. Python package (with the [bench] hardware extra)"
if python3 -c 'import tools.hw_config' 2>/dev/null; then
    c_ok "tools.* importable"
else
    c_do "pip install -e '.[bench]' --break-system-packages"
    run "cd '$REPO_ROOT' && pip install -e '.[bench]' --break-system-packages -q"
fi

# ---- 4. device-tree overlay + boot config -------------------------------
step "4. Device-tree overlay and boot config"
if [ -f /boot/firmware/overlays/mcp2515-triple.dtbo ]; then
    c_ok "mcp2515-triple.dtbo installed"
else
    c_do "compile and install mcp2515-triple.dtbo"
    run "dtc -@ -I dts -O dtb -o '$REPO_ROOT/infra/devicetree/mcp2515-triple.dtbo' '$REPO_ROOT/infra/devicetree/mcp2515-triple.dts' 2>/dev/null"
    run "sudo cp '$REPO_ROOT/infra/devicetree/mcp2515-triple.dtbo' /boot/firmware/overlays/"
fi

if [ -f "$BOOT_CONFIG.pre-hil" ]; then
    c_ok "rollback copy $BOOT_CONFIG.pre-hil exists"
elif grep -qE '^dtoverlay=mcp2515-triple' "$BOOT_CONFIG"; then
    # Copying now would file the ALREADY-MODIFIED config under a name that
    # claims to predate the modification -- worse than having no backup.
    c_skip "no pre-hil backup, and config.txt is already modified: not creating"
    c_skip "  a misleading one. Roll back by hand if you need to."
else
    c_do "back up $BOOT_CONFIG -> $BOOT_CONFIG.pre-hil"
    run "sudo cp '$BOOT_CONFIG' '$BOOT_CONFIG.pre-hil'"
fi

# spi0-0cs conflicts with our overlay: both claim SPI0.
if grep -qE '^dtoverlay=spi0-0cs' "$BOOT_CONFIG"; then
    c_do "comment out conflicting dtoverlay=spi0-0cs"
    run "sudo sed -i 's/^dtoverlay=spi0-0cs/#dtoverlay=spi0-0cs  # disabled by bench_bootstrap/' '$BOOT_CONFIG'"
else
    c_ok "no conflicting spi0-0cs overlay"
fi
for line in 'dtoverlay=mcp2515-triple' 'gpio=7=op,dl' 'gpio=8=ip,pd'; do
    if grep -qxF "$line" "$BOOT_CONFIG"; then
        c_ok "config.txt has $line"
    else
        c_do "append $line"
        run "echo '$line' | sudo tee -a '$BOOT_CONFIG' >/dev/null"
    fi
done

# ---- 5. patched kernel module ------------------------------------------
step "5. Patched mcp251x kernel module"
# The stock driver does not probe this hardware. Identify the patched build by
# comparing against the .orig the build script preserves -- NOT by grepping the
# binary for markers, which are C comments the compiler strips.
if [ -f "$MODULE.orig" ] \
   && [ "$(sudo md5sum "$MODULE" | cut -d' ' -f1)" != "$(sudo md5sum "$MODULE.orig" | cut -d' ' -f1)" ]; then
    c_ok "patched module installed (differs from stock)"
else
    c_do "build and install the patched mcp251x (compiles on this Pi, several minutes)"
    run "cd '$REPO_ROOT/infra/kernel-module/mcp251x-patched' && ./build.sh"
fi

# ---- 6. sudoers ---------------------------------------------------------
step "6. sudoers drop-in (narrow: ip link set canN only)"
if sudo -n -l 2>/dev/null | grep -q 'ip link set can'; then
    c_ok "ip-link escalation already granted"
else
    c_do "install /etc/sudoers.d/hil-broker"
    run "sudo install -m 0440 -o root -g root '$REPO_ROOT/infra/sudoers.d/hil-broker' /etc/sudoers.d/hil-broker"
    run "sudo visudo -c >/dev/null"
fi

# ---- 7. systemd units ---------------------------------------------------
step "7. systemd units (enabled, not started — they need the reboot)"
UNITS="hil-psu-on hil-can-up hil-broker hil-dashboard"
NEED_RELOAD=0
for u in $UNITS; do
    if [ -f "/etc/systemd/system/$u.service" ] \
       && cmp -s "/etc/systemd/system/$u.service" "$REPO_ROOT/infra/systemd/$u.service"; then
        c_ok "$u.service installed and current"
    elif [ -f "/etc/systemd/system/$u.service" ]; then
        c_do "REPLACE $u.service (installed copy differs from this checkout)"
        run "sudo cp '$REPO_ROOT/infra/systemd/$u.service' /etc/systemd/system/"
        NEED_RELOAD=1
    else
        c_do "install $u.service"
        run "sudo cp '$REPO_ROOT/infra/systemd/$u.service' /etc/systemd/system/"
        NEED_RELOAD=1
    fi
done
# hil-agent.service is intentionally not installed — see infra/systemd/README.md
if [ "$NEED_RELOAD" = 1 ]; then run "sudo systemctl daemon-reload"; fi
for u in $UNITS; do
    if [ "$(systemctl is-enabled "$u" 2>/dev/null)" = "enabled" ]; then
        c_ok "$u enabled"
    else
        c_do "enable $u"
        run "sudo systemctl enable '$u' >/dev/null 2>&1"
    fi
done

# ---- summary ------------------------------------------------------------
printf '\n%s\n' "------------------------------------------------------------"
if [ "$DRY" = 1 ]; then
    if [ "$CHANGED" = 0 ]; then
        echo "DRY RUN: nothing to do — this bench is already bootstrapped."
    else
        echo "DRY RUN: $CHANGED step(s) would change. Re-run without --dry-run."
    fi
    exit 0
fi

if [ "$CHANGED" = 0 ]; then
    echo "Nothing changed — this bench was already bootstrapped."
else
    echo "$CHANGED step(s) applied."
fi

cat <<'NEXT'

Next, in order:

  1. sudo reboot                       # overlay + module only load on boot
  2. python3 -m tools.bench doctor     # every check should pass

Then the parts this script deliberately leaves to you:

  - install can-flasher            docs/getting-started.md section 10
                                   (needs `gh` auth to a private repo)
  - write this bench's descriptor  python3 -m tools.bench describe --draft bench-NN
                                   then fill in the FIXMEs and `validate`
  - confirm it is honest           python3 -m tools.bench verify --bench bench-NN
  - register a runner              docs/getting-started.md section 14

Stimulus hardware (Pico LTC emulator, NTC interposer, pack-current fixture) is
not covered by this script or by the bringup guide. Without it a bench can only
honestly declare `dut-*` capabilities.
NEXT
