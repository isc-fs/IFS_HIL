#!/usr/bin/env bash
# Sync the local working copy to the HIL bench Pi via rsync.
#
# Use this instead of `git pull` on the Pi — the bench Pi's
# ~/IFS_HIL/ is a NON-git working copy maintained from a
# developer machine that has the real git checkout. Avoids storing
# GitHub credentials on the bench host and lets you test uncommitted
# changes against real hardware.
#
# Usage:
#   export HIL_BENCH_HOST=user@host          # pick the bench once per shell
#   scripts/sync_to_pi.sh                    # sync to $HIL_BENCH_HOST
#   scripts/sync_to_pi.sh --dry-run          # show changes, no transfer
#   scripts/sync_to_pi.sh user@host          # override for a single run
#   scripts/sync_to_pi.sh user@host --dry-run
#
# There is deliberately NO default host. With more than one bench on the
# fleet, silently syncing to someone else's bench is worse than an error.
# Bench addresses live in CLAUDE.md ("Bench hosts").
#
# Destination defaults to ~/IFS_HIL/ on the bench (relative to the remote
# user's home); override with HIL_BENCH_PATH if a bench differs.
#
# Auth:
#   - Preferred: per-developer SSH key (`ssh-copy-id "$HIL_BENCH_HOST"` once).
#   - Fallback: export HIL_SSH_PASS=<password> and the script uses sshpass.
#     Bootstrap only — never commit a password or share one across the team.
#
# What's excluded:
#   .git/, .DS_Store, __pycache__/, *.pyc, .pytest_cache/, build/,
#   docs/BACKPLANE_HIL/ (KiCad project, large), .claude/ (workstation-local).
#
# No --delete: Pi-side WIP (measurement output, ad-hoc scripts, etc.)
# is preserved across syncs. Run with --dry-run first if you're unsure.

set -euo pipefail

TARGET="${HIL_BENCH_HOST:-}"
DRY=""

for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY="--dry-run" ;;
        -h|--help)
            # Portable strip: BSD sed (macOS) has no \? operator, so the
            # old 's/^# \?//' silently left every line prefixed with '#'.
            sed -n '2,/^$/p' "$0" | sed -e 's/^# //' -e 's/^#$//'
            exit 0
            ;;
        *@*) TARGET="$arg" ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [user@host] [--dry-run]" >&2
            exit 2
            ;;
    esac
done

if [ -z "$TARGET" ]; then
    cat >&2 <<'EOF'
No bench selected.

  export HIL_BENCH_HOST=user@host      # once per shell, or
  scripts/sync_to_pi.sh user@host      # for a single run

Bench addresses are listed in CLAUDE.md ("Bench hosts"). There is no default
on purpose: with several benches on the fleet, syncing to the wrong one is
worse than an error.
EOF
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_PATH="${HIL_BENCH_PATH:-IFS_HIL/}"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

if [ -n "${HIL_SSH_PASS:-}" ]; then
    if ! command -v sshpass >/dev/null; then
        echo "HIL_SSH_PASS is set but sshpass is not installed (brew install hudochenkov/sshpass/sshpass)." >&2
        exit 1
    fi
    RSH="sshpass -p ${HIL_SSH_PASS} ssh ${SSH_OPTS}"
else
    RSH="ssh ${SSH_OPTS}"
fi

echo "Syncing ${REPO_ROOT}/  →  ${TARGET}:${DEST_PATH}  ${DRY:+(DRY RUN)}"

rsync -avh ${DRY} \
    --exclude='.git/' \
    --exclude='.DS_Store' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache/' \
    --exclude='build/' \
    --exclude='docs/BACKPLANE_HIL/' \
    --exclude='.claude/' \
    -e "${RSH}" \
    "${REPO_ROOT}/" \
    "${TARGET}:${DEST_PATH}"

echo
echo "✓ Sync complete."
echo
echo "If you changed broker/ or dashboard/ code, restart the services:"
echo "  ssh ${TARGET} 'sudo systemctl restart hil-broker hil-dashboard'"
