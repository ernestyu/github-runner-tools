#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/.github/actions/local-artifact-summary/summary.sh"
LIB="$ROOT/scripts/lib/archive-common.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT

for cmd in jq; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "SKIP: $cmd not installed"; exit 0; }
done

ARCHIVE="$TMP/archive"
ATTEMPT="$ARCHIVE/owner/repo/500/attempt_2"
mkdir -p "$ATTEMPT/job-a" "$ATTEMPT/job-b"

cat > "$ATTEMPT/job-a/manifest.json" <<'EOF'
{"archive_status":"PASS","repository":"owner/repo","run_id":"500","run_attempt":"2","file_count":3,"total_bytes":100}
EOF
cat > "$ATTEMPT/job-b/manifest.json" <<'EOF'
{"archive_status":"PASS","repository":"owner/repo","run_id":"500","run_attempt":"2","file_count":2,"total_bytes":50}
EOF

cat > "$TMP/archive.conf" <<EOF
ARCHIVE_ROOT=$ARCHIVE
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
EOF

SUMMARY="$TMP/summary.md"
: > "$SUMMARY"
env   GRT_TEST_MODE=1   GITHUB_ACTIONS=false   GRT_TEST_LIB="$LIB"   GRT_TEST_CONFIG="$TMP/archive.conf"   GITHUB_REPOSITORY=owner/repo   GITHUB_RUN_ID=500   GITHUB_RUN_ATTEMPT=2   GITHUB_SHA=abc123   GITHUB_STEP_SUMMARY="$SUMMARY"   bash "$SCRIPT"

grep -q 'Archive status: PASS' "$SUMMARY" || fail "fallback summary did not report PASS"
grep -q 'Jobs archived: 2' "$SUMMARY" || fail "fallback summary job count incorrect"
grep -q 'Files: 5' "$SUMMARY" || fail "fallback summary file count incorrect"
grep -q 'Size: 150 bytes' "$SUMMARY" || fail "fallback summary size incorrect"
grep -q 'archive://owner/repo/500/attempt_2' "$SUMMARY" || fail "fallback summary URI incorrect"

# A failure manifest must force FAILED and a non-zero action result.
cat > "$ATTEMPT/job-b/manifest.failed.json" <<'EOF'
{"archive_status":"FAILED","repository":"owner/repo","run_id":"500","run_attempt":"2","failure_code":"LOCAL_ARTIFACT_ARCHIVE_FAILED"}
EOF
: > "$SUMMARY"
if env   GRT_TEST_MODE=1   GITHUB_ACTIONS=false   GRT_TEST_LIB="$LIB"   GRT_TEST_CONFIG="$TMP/archive.conf"   GITHUB_REPOSITORY=owner/repo   GITHUB_RUN_ID=500   GITHUB_RUN_ATTEMPT=2   GITHUB_SHA=abc123   GITHUB_STEP_SUMMARY="$SUMMARY"   bash "$SCRIPT" >/dev/null 2>&1; then
  fail "fallback summary passed despite failure manifest"
fi
grep -q 'Archive status: FAILED' "$SUMMARY" || fail "fallback summary did not report FAILED"

# A mismatched manifest must be rejected rather than summarized.
rm -f "$ATTEMPT/job-b/manifest.failed.json"
cat > "$ATTEMPT/job-b/manifest.json" <<'EOF'
{"archive_status":"PASS","repository":"other/repo","run_id":"500","run_attempt":"2","file_count":2,"total_bytes":50}
EOF
: > "$SUMMARY"
if env   GRT_TEST_MODE=1   GITHUB_ACTIONS=false   GRT_TEST_LIB="$LIB"   GRT_TEST_CONFIG="$TMP/archive.conf"   GITHUB_REPOSITORY=owner/repo   GITHUB_RUN_ID=500   GITHUB_RUN_ATTEMPT=2   GITHUB_SHA=abc123   GITHUB_STEP_SUMMARY="$SUMMARY"   bash "$SCRIPT" >/dev/null 2>&1; then
  fail "fallback summary accepted mismatched manifest identity"
fi

echo "PASS: fallback local artifact summary tests"
