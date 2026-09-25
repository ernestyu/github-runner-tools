#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
ARCHIVE="$TMP/archive"
mkdir -p "$ARCHIVE"
LIB="$ROOT/scripts/lib/archive-common.sh"
HOOK="$TMP/hook.sh"
CONFIG="$TMP/archive.conf"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$HOOK"
chmod +x "$HOOK"
cat > "$CONFIG" <<EOF
ARCHIVE_ROOT=$ARCHIVE
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
EOF

LOCAL_ARCHIVE_LIB="$LIB"
LOCAL_ARCHIVE_HOOK="$HOOK"
LOCAL_ARCHIVE_CONFIG="$CONFIG"
RUNNER_USER="$(id -un)"
validate_local_archive_platform || fail "valid local archive platform rejected"

ENVFILE="$TMP/.env"
printf '%s\n' 'PATH=/usr/bin' > "$ENVFILE"
configure_runner_archive_hook "$ENVFILE"
grep -qx "ACTIONS_RUNNER_HOOK_JOB_COMPLETED=$HOOK" "$ENVFILE" || fail "registration hook not written"
grep -qx 'PATH=/usr/bin' "$ENVFILE" || fail "registration lost unrelated .env entry"
configure_runner_archive_hook "$ENVFILE"
[[ "$(grep -c '^ACTIONS_RUNNER_HOOK_JOB_COMPLETED=' "$ENVFILE")" -eq 1 ]] || fail "registration hook duplicated"

printf '%s\n' 'ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/other/hook' > "$ENVFILE"
if configure_runner_archive_hook "$ENVFILE" >/dev/null 2>&1; then fail "conflicting hook accepted"; fi

LOCAL_ARCHIVE_CONFIG="$TMP/missing.conf"
if validate_local_archive_platform >/dev/null 2>&1; then fail "missing config accepted"; fi

echo "PASS: runner registration archive integration tests"
