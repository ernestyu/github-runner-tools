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

die() { echo "ERROR: $*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

if [[ ${EUID} -eq 0 ]]; then
  die "Run setup-local-archive.sh as the normal runner owner, not with sudo. The script uses sudo only for host-level writes."
fi

RUNNER_USER="$(id -un)"
RUNNER_GROUP="$(id -gn)"
[[ "$ARCHIVE_ROOT" = /* ]] || die "RUNNER_ARCHIVE_ROOT must be an absolute path."
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ && "$RETENTION_DAYS" -ge 1 ]] || die "Retention days must be a positive integer."
[[ "$MIN_FREE_PERCENT" =~ ^[0-9]+$ && "$MIN_FREE_PERCENT" -ge 1 && "$MIN_FREE_PERCENT" -le 99 ]] || die "Minimum free percentage must be 1..99."
[[ "$COPY_TIMEOUT_SECONDS" =~ ^[0-9]+$ && "$COPY_TIMEOUT_SECONDS" -ge 1 ]] || die "Copy timeout must be a positive integer."

for cmd in sudo install mkdir id stat find jq rsync timeout df sha256sum; do require_command "$cmd"; done
[[ -r "$HOOK_SRC" && -r "$LIB_SRC" ]] || die "Run this script from a complete github-runner-tools checkout."

sudo -v || die "sudo access is required."

echo "Runner user : $RUNNER_USER"
echo "Runner group: $RUNNER_GROUP"
echo "Archive root: $ARCHIVE_ROOT"
echo "Hook path   : $HOOK_DST"
echo "Config path : $CONFIG_DST"

if [[ ! -e "$ARCHIVE_ROOT" ]]; then
  sudo install -d -o "$RUNNER_USER" -g "$RUNNER_GROUP" -m 0750 "$ARCHIVE_ROOT"
else
  [[ -d "$ARCHIVE_ROOT" ]] || die "Archive root exists but is not a directory: $ARCHIVE_ROOT"
fi

# Existing archive roots are never silently re-owned. They must already be usable.
[[ -x "$ARCHIVE_ROOT" && -r "$ARCHIVE_ROOT" && -w "$ARCHIVE_ROOT" ]] || die "Archive root must be traversable/readable/writable by $RUNNER_USER."
MODE="$(stat -c '%a' "$ARCHIVE_ROOT")"
OTHER_DIGIT="${MODE: -1}"
(( 10#$OTHER_DIGIT < 2 )) || die "Archive root must not be world-writable: mode=$MODE"

sudo install -d -o root -g root -m 0755 /usr/local/lib/github-runner-tools/hooks
sudo install -d -o root -g root -m 0755 /etc/github-runner-tools
sudo install -o root -g root -m 0755 "$HOOK_SRC" "$HOOK_DST"
sudo install -o root -g root -m 0644 "$LIB_SRC" "$LIB_DST"

TMP_CONFIG="$(mktemp)"
trap 'rm -f -- "$TMP_CONFIG"' EXIT
cat > "$TMP_CONFIG" <<EOF
ARCHIVE_ROOT=$ARCHIVE_ROOT
RETENTION_DAYS=$RETENTION_DAYS
MIN_FREE_PERCENT=$MIN_FREE_PERCENT
COPY_TIMEOUT_SECONDS=$COPY_TIMEOUT_SECONDS
EOF
sudo install -o root -g root -m 0644 "$TMP_CONFIG" "$CONFIG_DST"

[[ -r "$CONFIG_DST" ]] || die "Runner user cannot read archive config."
[[ ! -w "$CONFIG_DST" ]] || die "Runner user unexpectedly can write archive config."
[[ -r "$HOOK_DST" && -x "$HOOK_DST" ]] || die "Runner user cannot read/execute shared hook."
[[ ! -w "$HOOK_DST" ]] || die "Runner user unexpectedly can write shared hook."
[[ -r "$LIB_DST" ]] || die "Runner user cannot read shared library."
[[ ! -w "$LIB_DST" ]] || die "Runner user unexpectedly can write shared library."

PROBE="$ARCHIVE_ROOT/.github-runner-tools-write-test.$$"
: > "$PROBE" || die "Runner user cannot create files in archive root."
rm -f -- "$PROBE"

echo
echo "Local artifact platform setup complete."
echo "Next: register a new runner or run scripts/enable-local-archive.sh --dry-run for existing runners."
