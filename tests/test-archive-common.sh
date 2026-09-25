#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/lib/archive-common.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_eq() { [[ "$1" == "$2" ]] || fail "expected '$2', got '$1'"; }

TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT

assert_eq "$(grt_sanitize_component 'ErnestYu')" "ernestyu"
assert_eq "$(grt_sanitize_component 'Project_A')" "project_a"
if grt_sanitize_component '..' >/dev/null 2>&1; then fail "accepted .. path component"; fi
if grt_sanitize_component 'a/b' >/dev/null 2>&1; then fail "accepted slash in path component"; fi

grt_parse_repository "ErnestYu/Project_A" || fail "valid repository rejected"
assert_eq "$GRT_OWNER_PATH" "ernestyu"
assert_eq "$GRT_REPO_PATH" "project_a"
if grt_parse_repository "bad/repo/extra" >/dev/null 2>&1; then fail "invalid repository accepted"; fi

GITHUB_RUN_ID=123 GITHUB_RUN_ATTEMPT=2 GITHUB_JOB=test
grt_validate_run_identity || fail "valid run identity rejected"
assert_eq "$GRT_JOB_SAFE" "test"
GITHUB_RUN_ID=x
if grt_validate_run_identity >/dev/null 2>&1; then fail "non-numeric run id accepted"; fi

mkdir -p "$TMP/archive/sub"
ROOT_CANON="$(grt_canonical_dir "$TMP/archive")"
SUB_CANON="$(grt_canonical_dir "$TMP/archive/sub")"
grt_assert_beneath "$ROOT_CANON" "$SUB_CANON" || fail "valid child rejected"
if grt_assert_beneath "$ROOT_CANON" "$TMP" >/dev/null 2>&1; then fail "path escape accepted"; fi

assert_eq "$(grt_archive_uri_for ernestyu repo 123 2 test)" "archive://ernestyu/repo/123/attempt_2/test"

cat > "$TMP/archive.conf" <<EOF
ARCHIVE_ROOT=$ROOT_CANON
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
EOF
grt_load_archive_config "$TMP/archive.conf" || fail "valid config rejected"
assert_eq "$ARCHIVE_ROOT" "$ROOT_CANON"
assert_eq "$RETENTION_DAYS" "90"

cat > "$TMP/bad.conf" <<EOF
ARCHIVE_ROOT=relative
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
EOF
if grt_load_archive_config "$TMP/bad.conf" >/dev/null 2>&1; then fail "relative archive root accepted"; fi

ENVFILE="$TMP/.env"
printf '%s\n' 'PATH=/usr/bin' > "$ENVFILE"
grt_set_runner_env_value "$ENVFILE" ACTIONS_RUNNER_HOOK_JOB_COMPLETED /hook
assert_eq "$(grt_read_runner_env_value "$ENVFILE" ACTIONS_RUNNER_HOOK_JOB_COMPLETED)" "/hook"
grep -qx 'PATH=/usr/bin' "$ENVFILE" || fail "unrelated env entry lost"
grt_set_runner_env_value "$ENVFILE" ACTIONS_RUNNER_HOOK_JOB_COMPLETED /hook
if grt_set_runner_env_value "$ENVFILE" ACTIONS_RUNNER_HOOK_JOB_COMPLETED /other >/dev/null 2>&1; then fail "conflicting env value overwritten"; fi

echo "PASS: archive common helper tests"
