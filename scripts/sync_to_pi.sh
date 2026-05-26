#!/usr/bin/env bash
# Sync the local working copy to the HIL bench Pi via rsync.
#
# Use this instead of `git pull` on the Pi — the bench Pi's
# /home/isc/IFS08_HIL/ is a NON-git working copy maintained from a
# developer machine that has the real git checkout. Avoids storing
# GitHub credentials on the bench host and lets you test uncommitted
# changes against real hardware.
#
# Usage:
#   scripts/sync_to_pi.sh                    # default isc@100.96.95.78
#   scripts/sync_to_pi.sh --dry-run          # show changes, no transfer
#   scripts/sync_to_pi.sh user@host          # custom target
#   scripts/sync_to_pi.sh user@host --dry-run
#
# Auth:
#   - Preferred: SSH key auth (run `ssh-copy-id isc@100.96.95.78` once).
#   - Fallback: export HIL_SSH_PASS=<password> and the script uses sshpass.
#
# What's excluded:
#   .git/, .DS_Store, __pycache__/, *.pyc, .pytest_cache/, build/,
#   docs/BACKPLANE_HIL/ (KiCad project, large), .claude/ (workstation-local).
#
# No --delete: Pi-side WIP (measurement output, ad-hoc scripts, etc.)
# is preserved across syncs. Run with --dry-run first if you're unsure.

set -euo pipefail

DEFAULT_TARGET="isc@100.96.95.78"
TARGET="$DEFAULT_TARGET"
DRY=""

for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY="--dry-run" ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
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

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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

echo "Syncing ${REPO_ROOT}/  →  ${TARGET}:/home/isc/IFS08_HIL/  ${DRY:+(DRY RUN)}"

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
    "${TARGET}:/home/isc/IFS08_HIL/"

echo
echo "✓ Sync complete."
echo
echo "If you changed broker/ or dashboard/ code, restart the services:"
echo "  ssh ${TARGET} 'sudo systemctl restart hil-broker hil-dashboard'"
