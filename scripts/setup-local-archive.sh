#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_SRC="$ROOT/hooks/archive-job-completed.sh"
LIB_SRC="$ROOT/scripts/lib/archive-common.sh"

ARCHIVE_ROOT="${RUNNER_ARCHIVE_ROOT:-/srv/github-actions-archive}"
RETENTION_DAYS="${RUNNER_ARCHIVE_RETENTION_DAYS:-90}"
MIN_FREE_PERCENT="${RUNNER_ARCHIVE_MIN_FREE_PERCENT:-15}"
COPY_TIMEOUT_SECONDS="${RUNNER_ARCHIVE_COPY_TIMEOUT_SECONDS:-3600}"

HOOK_DST="/usr/local/lib/github-runner-tools/hooks/archive-job-completed.sh"
LIB_DST="/usr/local/lib/github-runner-tools/archive-common.sh"
CONFIG_DST="/etc/github-runner-tools/archive.conf"
MODE="dry-run"

usage() {
  cat <<'USAGE'
Usage:
  setup-local-archive.sh [--dry-run|--apply]

Default: --dry-run

Run as the normal Linux user that owns the self-hosted runners.
Do not run the whole script with sudo.
USAGE
}

die() { echo "ERROR: $*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) MODE="dry-run"; shift ;;
    --apply) MODE="apply"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

if [[ ${EUID} -eq 0 ]]; then
  die "Run setup-local-archive.sh as the normal runner owner, not with sudo. The script uses sudo only for host-level writes."
fi

RUNNER_USER="$(id -un)"
RUNNER_GROUP="$(id -gn)"

[[ "$ARCHIVE_ROOT" = /* && "$ARCHIVE_ROOT" != "/" ]] || die "RUNNER_ARCHIVE_ROOT must be an absolute non-root path."
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ && "$RETENTION_DAYS" -ge 1 ]] || die "Retention days must be a positive integer."
[[ "$MIN_FREE_PERCENT" =~ ^[0-9]+$ && "$MIN_FREE_PERCENT" -ge 1 && "$MIN_FREE_PERCENT" -le 99 ]] || die "Minimum free percentage must be 1..99."
[[ "$COPY_TIMEOUT_SECONDS" =~ ^[0-9]+$ && "$COPY_TIMEOUT_SECONDS" -ge 1 ]] || die "Copy timeout must be a positive integer."

for cmd in sudo install id stat jq rsync timeout df sha256sum grep mktemp flock; do
  require_command "$cmd"
done
[[ -r "$HOOK_SRC" && -r "$LIB_SRC" ]] || die "Run this script from a complete github-runner-tools checkout."

sudo -v || die "sudo access is required."

echo "Mode        : $MODE"
echo "Runner user : $RUNNER_USER"
echo "Runner group: $RUNNER_GROUP"
echo "Archive root: $ARCHIVE_ROOT"
echo "Hook path   : $HOOK_DST"
echo "Config path : $CONFIG_DST"
echo "Retention   : $RETENTION_DAYS days"
echo "Disk guard  : $MIN_FREE_PERCENT% minimum free"
echo "Copy timeout: $COPY_TIMEOUT_SECONDS seconds"

if [[ -e "$ARCHIVE_ROOT" ]]; then
  [[ -d "$ARCHIVE_ROOT" && ! -L "$ARCHIVE_ROOT" ]] || die "Archive root must be a real directory, not a symlink: $ARCHIVE_ROOT"
  [[ -x "$ARCHIVE_ROOT" && -r "$ARCHIVE_ROOT" && -w "$ARCHIVE_ROOT" ]] ||     die "Existing archive root must be traversable/readable/writable by $RUNNER_USER."
  MODE_BITS="$(stat -c '%a' "$ARCHIVE_ROOT")"
  OTHER_DIGIT="${MODE_BITS: -1}"
  (( (10#$OTHER_DIGIT & 2) == 0 )) || die "Archive root must not be world-writable: mode=$MODE_BITS"
else
  echo "Archive root: would create owner=$RUNNER_USER group=$RUNNER_GROUP mode=0750"
fi

if [[ -e "$HOOK_DST" ]] && ! grep -q '^# github-runner-tools-managed-hook$' "$HOOK_DST" 2>/dev/null; then
  die "Refusing to replace an unmanaged completed hook at $HOOK_DST"
fi
if [[ -e "$LIB_DST" ]] && ! grep -q '^# github-runner-tools-managed-library$' "$LIB_DST" 2>/dev/null; then
  die "Refusing to replace an unmanaged shared library at $LIB_DST"
fi
if [[ -e "$CONFIG_DST" ]] && ! grep -q '^# managed-by=github-runner-tools$' "$CONFIG_DST" 2>/dev/null; then
  die "Refusing to replace an unmanaged archive config at $CONFIG_DST"
fi

if [[ "$MODE" == "dry-run" ]]; then
  echo
  echo "DRY-RUN: no host files were changed."
  echo "Run again with --apply after reviewing the configuration."
  exit 0
fi

if [[ ! -e "$ARCHIVE_ROOT" ]]; then
  sudo install -d -o "$RUNNER_USER" -g "$RUNNER_GROUP" -m 0750 "$ARCHIVE_ROOT"
fi

sudo install -d -o root -g root -m 0755 /usr/local/lib/github-runner-tools/hooks
sudo install -d -o root -g root -m 0755 /etc/github-runner-tools
sudo install -o root -g root -m 0755 "$HOOK_SRC" "$HOOK_DST"
sudo install -o root -g root -m 0644 "$LIB_SRC" "$LIB_DST"

TMP_CONFIG="$(mktemp)"
trap 'rm -f -- "$TMP_CONFIG"' EXIT
cat > "$TMP_CONFIG" <<EOF
# managed-by=github-runner-tools
ARCHIVE_ROOT=$ARCHIVE_ROOT
RETENTION_DAYS=$RETENTION_DAYS
MIN_FREE_PERCENT=$MIN_FREE_PERCENT
COPY_TIMEOUT_SECONDS=$COPY_TIMEOUT_SECONDS
EOF
sudo install -o root -g root -m 0644 "$TMP_CONFIG" "$CONFIG_DST"

[[ "$(stat -c '%u' "$CONFIG_DST")" == "0" ]] || die "Archive config is not root-owned."
[[ "$(stat -c '%u' "$HOOK_DST")" == "0" ]] || die "Shared hook is not root-owned."
[[ "$(stat -c '%u' "$LIB_DST")" == "0" ]] || die "Shared library is not root-owned."

[[ -r "$CONFIG_DST" && ! -w "$CONFIG_DST" ]] || die "Runner user must read but not write archive config."
[[ -r "$HOOK_DST" && -x "$HOOK_DST" && ! -w "$HOOK_DST" ]] || die "Runner user must read/execute but not write shared hook."
[[ -r "$LIB_DST" && ! -w "$LIB_DST" ]] || die "Runner user must read but not write shared library."

PROBE="$ARCHIVE_ROOT/.github-runner-tools-write-test.$$"
: > "$PROBE" || die "Runner user cannot create files in archive root."
rm -f -- "$PROBE"

echo
echo "Local artifact platform setup complete."
echo "Next: register a new runner or run scripts/enable-local-archive.sh --dry-run for existing runners."
