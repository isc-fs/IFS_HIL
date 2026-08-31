#!/usr/bin/env bash
# Take a Raspberry Pi from freshly imaged to a bench registered with the fleet.
#
# This is THE script a new bench owner runs. It is resumable: the setup spans a
# reboot, so state is kept and re-running continues where it stopped.
#
#   scripts/bench_setup.sh --bench bench-02 --dry-run   # report, change nothing
#   scripts/bench_setup.sh --bench bench-02             # do it
#   sudo reboot                                         # when it asks
#   scripts/bench_setup.sh --bench bench-02             # continues after reboot
#
# Phases:
#   base        interfaces, packages, python, overlay, kernel module, sudoers,
#               systemd units          (docs/getting-started.md 1-7)
#   reboot      required: the overlay and patched module only load on boot
#   host        `bench doctor` -- the host matches the documented build
#   flasher     can-flasher            (needs `gh` auth; skipped if absent)
#   descriptor  draft, then YOU fill the FIXMEs, then validate + verify
#   runner      register a self-hosted runner with this bench's labels
#
# Options:
#   --bench ID          bench id, e.g. bench-02 (required past the base phase)
#   --dry-run, -n       report what would change, touch nothing
#   --yes, -y           do not prompt (still will not reboot for you)
#   --runner-token TOK  registration token; otherwise minted via `gh` if available
#   --restart           forget saved progress and start from the top
#
# Idempotent throughout: every step checks first and reports "ok" when already
# done, so re-running is always safe.
#
# What it will NOT do: burn a carrier's bootloader (SWD, out of band), decide
# what your bench is wired to, or reboot without being told.

set -euo pipefail

BENCH_ID=""
DRY=0
ASSUME_YES=0
RUNNER_TOKEN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --bench)        BENCH_ID="${2:-}"; shift 2 ;;
        --runner-token) RUNNER_TOKEN="${2:-}"; shift 2 ;;
        --dry-run|-n)   DRY=1; shift ;;
        --yes|-y)       ASSUME_YES=1; shift ;;
        --restart)      rm -f "$HOME/.hil-bench-setup"; shift ;;
        -h|--help)      sed -n '2,/^$/p' "$0" | sed -e 's/^# //' -e 's/^#$//'; exit 0 ;;
        *)              echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOOT_CONFIG=/boot/firmware/config.txt
KVER="$(uname -r)"
MODULE="/lib/modules/$KVER/kernel/drivers/net/can/spi/mcp251x.ko.xz"
STATE="$HOME/.hil-bench-setup"
CHANGED=0

c_ok()   { printf '  [32mok[0m    %s
' "$*"; }
c_do()   { printf '  [33mdo[0m    %s
' "$*"; CHANGED=$((CHANGED+1)); }
c_skip() { printf '  --    %s
' "$*"; }
c_stop() { printf '
[33m>> %s[0m
' "$*"; }
step()   { printf '
[1m%s[0m
' "$*"; }

run() { if [ "$DRY" = 1 ]; then return 0; fi; eval "$@"; }
phase_done() { grep -qx "$1" "$STATE" 2>/dev/null; }
mark_done()  { [ "$DRY" = 1 ] || { touch "$STATE"; grep -qx "$1" "$STATE" || echo "$1" >> "$STATE"; }; }
need_bench() {
    [ -n "$BENCH_ID" ] || { echo "!! --bench <id> is required for this phase" >&2; exit 2; }
}

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
printf '\nbench setup — %s, kernel %s%s\n' "$(uname -n)" "$KVER" "${BENCH_ID:+, bench $BENCH_ID}"

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
# gcc-arm-none-eabi + cmake are for the artifact FALLBACK: when a cloud build
# cannot hand its artifact to the bench (a full GitHub artifact quota is the
# known case), hil-test.yml rebuilds the same reviewed commit here instead of
# taking the bench offline. Without them that path fails at the compile.
PKGS="python3-can can-utils device-tree-compiler xz-utils libudev-dev pkg-config git curl gcc-arm-none-eabi cmake linux-headers-$KVER"
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
    run "sudo sed -i 's/^dtoverlay=spi0-0cs/#dtoverlay=spi0-0cs  # disabled by bench_setup/' '$BOOT_CONFIG'"
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

# ---- reboot gate --------------------------------------------------------
step "Reboot gate"
if [ -e /dev/spidev0.3 ] && ip link show can2 >/dev/null 2>&1; then
    c_ok "overlay live and CAN interfaces present — no reboot needed"
    mark_done reboot
else
    printf '
%s
' "------------------------------------------------------------"
    c_stop "Reboot required before anything else can be checked."
    cat <<'MSG'
   The device-tree overlay and the patched mcp251x module only load at boot,
   so nothing below can be verified until this Pi has restarted.

       sudo reboot

   Then re-run this script with the same arguments; it continues from here.
MSG
    exit 0
fi

# ---- host build ---------------------------------------------------------
step "Host build check"
if [ "$DRY" = 1 ]; then
    c_skip "would run: python3 -m tools.bench doctor"
elif ( cd "$REPO_ROOT" && python3 -m tools.bench doctor >/tmp/hil-doctor.out 2>&1 ); then
    c_ok "matches the documented build"
    mark_done host
else
    sed 's/^/    /' /tmp/hil-doctor.out
    c_stop "doctor found problems — fix them before continuing (see the section numbers above)."
    exit 1
fi

# ---- can-flasher --------------------------------------------------------
step "can-flasher"
# 2.8.0 is the floor, not a preference. Older builds have no ISO-TP session
# recovery (isc-fs/MingoCAN#506, fixed by #527) and NACK BAD_SESSION part-way
# through a large image -- AFTER the erase, so the carrier is left with no app.
# bench-01 sat on 2.5.5 precisely because this step used to accept ANY existing
# install, and it wiped the AMS before anyone noticed.
FLASHER_MIN="2.8.0"
_ver_lt() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ] && [ "$1" != "$2" ]; }

if command -v can-flasher >/dev/null 2>&1 && \
   ! _ver_lt "$(can-flasher --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" "$FLASHER_MIN"; then
    c_ok "$(can-flasher --version 2>/dev/null | head -1)"
    mark_done flasher
elif command -v can-flasher >/dev/null 2>&1 && (! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1); then
    c_skip "can-flasher $(can-flasher --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1) is older than $FLASHER_MIN and cannot"
    c_skip "  finish a large flash — it erases, then fails. Upgrade it before flashing:"
    c_skip "  docs/getting-started.md section 10."
elif ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
    # A private release: without gh auth here it must come from a workstation.
    c_skip "not installed, and gh is unavailable or unauthenticated on this Pi."
    c_skip "  Install from a workstation — docs/getting-started.md section 10."
else
    c_do "download and install the current can-flasher release"
    run "VER=\$(gh release view -R isc-fs/MingoCAN --json tagName --jq .tagName) && \
         cd /tmp && \
         gh release download \"\$VER\" -R isc-fs/MingoCAN -p \"can-flasher-\$VER-aarch64-unknown-linux-gnu.tar.gz\" --clobber && \
         tar -xzf can-flasher-\$VER-aarch64-unknown-linux-gnu.tar.gz && \
         sudo install -m 0755 can-flasher-\$VER-aarch64-unknown-linux-gnu/can-flasher /usr/local/bin/"
    mark_done flasher
fi

# ---- descriptor ---------------------------------------------------------
need_bench
DESC="$REPO_ROOT/configs/benches/$BENCH_ID.yaml"
step "Bench descriptor — $BENCH_ID"
if [ ! -f "$DESC" ]; then
    c_do "draft $DESC from a live probe of this bench"
    run "cd '$REPO_ROOT' && python3 -m tools.bench describe --draft '$BENCH_ID' --out '$DESC'"
    c_stop "Draft written. This is the part no script can do for you."
    cat <<MSG
   Edit $DESC:
     - fill every FIXME (owner, hosts, board_rev, routing)
     - declare ONLY capabilities this bench really has. These labels route
       other people's test runs; an optimistic one silently attracts work
       this bench cannot serve, which is why the draft leaves them empty.

   Then re-run this script to validate and verify it.
MSG
    exit 0
fi
if grep -q 'FIXME' "$DESC"; then
    c_stop "$DESC still contains FIXME — fill it in, then re-run."
    grep -n 'FIXME' "$DESC" | head -8 | sed 's/^/    /'
    exit 0
fi
if [ "$DRY" = 1 ]; then
    c_skip "would validate and verify $BENCH_ID"
else
    ( cd "$REPO_ROOT" && python3 -m tools.bench validate ) || {
        c_stop "descriptor failed validation — fix it and re-run."; exit 1; }
    c_ok "descriptor valid"
    if ( cd "$REPO_ROOT" && python3 -m tools.bench verify --bench "$BENCH_ID" ); then
        c_ok "hardware matches the descriptor"
        mark_done descriptor
    else
        c_stop "the bench does not match its own descriptor. Correct whichever is"
        echo "   wrong -- the wiring or the description -- then re-run."
        exit 1
    fi
fi

# ---- runner -------------------------------------------------------------
step "Self-hosted runner"
LABELS="$( cd "$REPO_ROOT" && python3 -m tools.bench labels --bench "$BENCH_ID" 2>/dev/null || true )"
if [ -f "$HOME/actions-runner/.runner" ]; then
    c_ok "runner already configured"
    if systemctl list-units --type=service --all 2>/dev/null | grep -q actions.runner; then
        c_ok "runner service present"
    else
        c_do "install and start the runner service"
        run "cd '$HOME/actions-runner' && sudo ./svc.sh install \"$(id -un)\" && sudo ./svc.sh start"
    fi
    mark_done runner
elif [ -z "$RUNNER_TOKEN" ] && ! ( command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 ); then
    c_skip "no --runner-token and gh is unavailable here."
    c_skip "  Mint one and re-run:"
    c_skip "    gh api -X POST repos/isc-fs/IFS_HIL/actions/runners/registration-token --jq .token"
    c_skip "  Labels this bench should register with:"
    c_skip "    $LABELS"
else
    c_do "download, configure and start a runner labelled: $LABELS"
    if [ -z "$RUNNER_TOKEN" ]; then
        run "RUNNER_TOKEN=\$(gh api -X POST repos/isc-fs/IFS_HIL/actions/runners/registration-token --jq .token)"
    fi
    # The asset name embeds the version, so .../latest/download/<name> 404s --
    # resolve the tag first. svc.sh does not exist until config.sh has run.
    run "mkdir -p '$HOME/actions-runner' && cd '$HOME/actions-runner' && \
         RUNNER_VER=\$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | sed -n 's/.*\"tag_name\": *\"v\\([^\"]*\\)\".*/\\1/p') && \
         curl -fsSL -o runner.tar.gz \"https://github.com/actions/runner/releases/download/v\${RUNNER_VER}/actions-runner-linux-arm64-\${RUNNER_VER}.tar.gz\" && \
         tar xzf runner.tar.gz"
    run "cd '$HOME/actions-runner' && ./config.sh --unattended --url https://github.com/isc-fs/IFS_HIL \
         --token \"\${RUNNER_TOKEN}\" --name '$BENCH_ID' --labels '$LABELS' --replace"
    run "cd '$HOME/actions-runner' && sudo ./svc.sh install \"$(id -un)\" && sudo ./svc.sh start"
    mark_done runner
fi

# ---- done ---------------------------------------------------------------
printf '
%s
' "------------------------------------------------------------"
if [ "$DRY" = 1 ]; then
    if [ "$CHANGED" = 0 ]; then
        echo "DRY RUN: nothing to do — this bench is fully set up."
    else
        echo "DRY RUN: $CHANGED step(s) would change. Re-run without --dry-run."
    fi
    exit 0
fi
if [ "$CHANGED" = 0 ]; then
    echo "Nothing changed — this bench was already fully set up."
else
    echo "$CHANGED step(s) applied."
fi
cat <<MSG

$BENCH_ID is set up. Commit its descriptor and open a PR:

    git add configs/benches/$BENCH_ID.yaml && git commit && gh pr create --base dev

Then a dispatched run should land here:

    gh workflow run hil-test.yml -f bench=$BENCH_ID -f suite=tests/hil/test_can.py

Stimulus hardware (Pico LTC emulator, NTC interposer, pack-current fixture) is
not covered by this script or by the bringup guide. Until it is documented, a
new bench can only honestly declare dut-* capabilities.
MSG
