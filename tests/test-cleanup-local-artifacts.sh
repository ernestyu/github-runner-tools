#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/cleanup-local-artifacts.sh"
fail() { echo "FAIL: $*" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
ARCHIVE="$TMP/archive"
mkdir -p "$ARCHIVE/owner/repo/1/attempt_1/job" "$ARCHIVE/owner/repo/2/attempt_1/job" "$ARCHIVE/owner/repo/3/attempt_1/job"

old="$(date -u -d '200 days ago' +%Y-%m-%dT%H:%M:%SZ)"
new="$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ)"
for run in 1 2 3; do
  ts="$old"; [[ "$run" == 2 ]] && ts="$new"
  cat > "$ARCHIVE/owner/repo/$run/attempt_1/job/manifest.json" <<EOF
{"completed_at_utc":"$ts","archive_status":"PASS"}
EOF
done
: > "$ARCHIVE/owner/repo/3/.keep"

cat > "$TMP/archive.conf" <<EOF
ARCHIVE_ROOT=$ARCHIVE
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
EOF

out="$(GRT_TEST_MODE=1 GITHUB_ACTIONS=false GRT_TEST_CONFIG="$TMP/archive.conf" bash "$SCRIPT" --dry-run --verbose)"
grep -q "WOULD DELETE: $ARCHIVE/owner/repo/1" <<<"$out" || fail "old run not selected"
grep -q "SKIP recent: $ARCHIVE/owner/repo/2" <<<"$out" || fail "recent run not retained"
grep -q "SKIP kept: $ARCHIVE/owner/repo/3" <<<"$out" || fail ".keep did not protect run"
[[ -d "$ARCHIVE/owner/repo/1" ]] || fail "dry-run deleted data"

GRT_TEST_MODE=1 GITHUB_ACTIONS=false GRT_TEST_CONFIG="$TMP/archive.conf" bash "$SCRIPT" --apply >/dev/null
[[ ! -e "$ARCHIVE/owner/repo/1" ]] || fail "apply did not delete eligible run"
[[ -d "$ARCHIVE/owner/repo/2" ]] || fail "apply deleted recent run"
[[ -d "$ARCHIVE/owner/repo/3" ]] || fail "apply deleted kept run"

mkdir -p "$ARCHIVE/owner/repo/4/attempt_1/job/.workspace.tmp.1"
cat > "$ARCHIVE/owner/repo/4/attempt_1/job/manifest.json" <<EOF
{"completed_at_utc":"$old","archive_status":"PASS"}
EOF
GRT_TEST_MODE=1 GITHUB_ACTIONS=false GRT_TEST_CONFIG="$TMP/archive.conf" bash "$SCRIPT" --apply >/dev/null
[[ -d "$ARCHIVE/owner/repo/4" ]] || fail "active staging run was deleted"

echo "PASS: cleanup retention tests"
