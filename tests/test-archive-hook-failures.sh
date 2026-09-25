#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$ROOT/hooks/archive-job-completed.sh"
LIB="$ROOT/scripts/lib/archive-common.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
ARCHIVE="$TMP/archive"
WORK="$TMP/work"
mkdir -p "$ARCHIVE" "$WORK"

write_config() {
  local min_free="${1:-1}" timeout_seconds="${2:-30}"
  cat > "$TMP/archive.conf" <<EOF
ARCHIVE_ROOT=$ARCHIVE
RETENTION_DAYS=90
MIN_FREE_PERCENT=$min_free
COPY_TIMEOUT_SECONDS=$timeout_seconds
EOF
}

run_hook() {
  local run_id="$1" workspace="$2" summary="$3"
  shift 3
  env     GRT_TEST_MODE=1     GRT_TEST_LIB="$LIB"     GRT_TEST_CONFIG="$TMP/archive.conf"     GITHUB_ACTIONS=false     GITHUB_REPOSITORY="owner/repo"     GITHUB_RUN_ID="$run_id"     GITHUB_RUN_ATTEMPT=1     GITHUB_JOB=test     GITHUB_SHA=abcdef123     GITHUB_REF=refs/heads/main     GITHUB_WORKFLOW=CI     GITHUB_WORKSPACE="$workspace"     RUNNER_NAME=local-ci-test     GITHUB_STEP_SUMMARY="$summary"     "$@"     bash "$HOOK"
}

# Empty workspaces are valid and create a zero-file PASS archive.
write_config 1 30
SUMMARY="$TMP/empty-summary.md"
: > "$SUMMARY"
run_hook 201 "$WORK" "$SUMMARY" >/dev/null
M="$(find "$ARCHIVE/owner/repo/201/attempt_1" -name manifest.json -type f -print -quit)"
[[ -n "$M" ]] || fail "empty workspace did not produce a PASS manifest"
[[ "$(jq -r '.file_count' "$M")" == "0" ]] || fail "empty workspace did not record zero files"

# Missing workspace must fail after allocating a safe target and leave a failure manifest.
SUMMARY="$TMP/missing-summary.md"
: > "$SUMMARY"
if run_hook 202 "$TMP/does-not-exist" "$SUMMARY" >/dev/null 2>&1; then
  fail "missing workspace unexpectedly passed"
fi
FM="$(find "$ARCHIVE/owner/repo/202/attempt_1" -name manifest.failed.json -type f -print -quit)"
[[ -n "$FM" ]] || fail "missing workspace did not leave a failure manifest"
[[ "$(jq -r '.failure_code' "$FM")" == "LOCAL_ARTIFACT_WORKSPACE_INVALID" ]] || fail "wrong missing-workspace failure code"
grep -q 'Archive status: FAILED' "$SUMMARY" || fail "missing workspace did not write FAILED summary"

# Workflow-level RUNNER_ARCHIVE_ROOT must not override the root-owned/test config.
printf 'safe\n' > "$WORK/result.txt"
SUMMARY="$TMP/override-summary.md"
: > "$SUMMARY"
run_hook 203 "$WORK" "$SUMMARY" RUNNER_ARCHIVE_ROOT="$TMP/evil" >/dev/null
[[ -d "$ARCHIVE/owner/repo/203" ]] || fail "configured archive root was not used"
[[ ! -e "$TMP/evil" ]] || fail "workflow RUNNER_ARCHIVE_ROOT affected destination"

# Disk guard failure must happen before copy and leave no PASS manifest.
MOCKBIN="$TMP/mockbin"
mkdir -p "$MOCKBIN"
cat > "$MOCKBIN/df" <<'MOCK'
#!/usr/bin/env bash
cat <<EOF
Filesystem 1024-blocks Used Available Capacity Mounted on
mockfs 1000 900 100 90% /archive
EOF
MOCK
chmod +x "$MOCKBIN/df"
write_config 15 30
SUMMARY="$TMP/disk-summary.md"
: > "$SUMMARY"
if PATH="$MOCKBIN:$PATH" run_hook 204 "$WORK" "$SUMMARY" >/dev/null 2>&1; then
  fail "low disk guard unexpectedly passed"
fi
FM="$(find "$ARCHIVE/owner/repo/204/attempt_1" -name manifest.failed.json -type f -print -quit)"
[[ -n "$FM" ]] || fail "disk guard did not leave a failure manifest"
[[ "$(jq -r '.failure_code' "$FM")" == "LOCAL_ARTIFACT_DISK_GUARD_FAILED" ]] || fail "wrong disk-guard failure code"
if find "$ARCHIVE/owner/repo/204" -name manifest.json -type f -print -quit | grep -q .; then
  fail "disk guard wrote a PASS manifest"
fi

# Non-timeout rsync failure must be a normal archive failure and preserve failed staging evidence.
cat > "$MOCKBIN/rsync" <<'MOCK'
#!/usr/bin/env bash
exit 23
MOCK
chmod +x "$MOCKBIN/rsync"
cat > "$MOCKBIN/df" <<'MOCK'
#!/usr/bin/env bash
cat <<EOF
Filesystem 1024-blocks Used Available Capacity Mounted on
mockfs 1000 100 900 10% /archive
EOF
MOCK
chmod +x "$MOCKBIN/df"
write_config 15 30
SUMMARY="$TMP/rsync-summary.md"
: > "$SUMMARY"
if PATH="$MOCKBIN:$PATH" run_hook 205 "$WORK" "$SUMMARY" >/dev/null 2>&1; then
  fail "rsync failure unexpectedly passed"
fi
FM="$(find "$ARCHIVE/owner/repo/205/attempt_1" -name manifest.failed.json -type f -print -quit)"
[[ "$(jq -r '.failure_code' "$FM")" == "LOCAL_ARTIFACT_ARCHIVE_FAILED" ]] || fail "wrong rsync failure code"
find "$ARCHIVE/owner/repo/205" -type d -name '.workspace.failed.*' -print -quit | grep -q . || fail "rsync failure did not preserve failed staging evidence"

# Timeout exit code must be classified separately.
cat > "$MOCKBIN/timeout" <<'MOCK'
#!/usr/bin/env bash
exit 124
MOCK
chmod +x "$MOCKBIN/timeout"
write_config 15 1
SUMMARY="$TMP/timeout-summary.md"
: > "$SUMMARY"
if PATH="$MOCKBIN:$PATH" run_hook 206 "$WORK" "$SUMMARY" >/dev/null 2>&1; then
  fail "timeout unexpectedly passed"
fi
FM="$(find "$ARCHIVE/owner/repo/206/attempt_1" -name manifest.failed.json -type f -print -quit)"
[[ "$(jq -r '.failure_code' "$FM")" == "LOCAL_ARTIFACT_ARCHIVE_TIMEOUT" ]] || fail "wrong timeout failure code"

# The same configured deadline must cover finalization after rsync succeeds.
# Mock find to block after the workspace has already been moved into place.
FINALBIN="$TMP/finalbin"
mkdir -p "$FINALBIN"
cat > "$FINALBIN/find" <<'MOCK'
#!/usr/bin/env bash
sleep 5
exit 0
MOCK
chmod +x "$FINALBIN/find"
write_config 1 1
SUMMARY="$TMP/finalize-timeout-summary.md"
: > "$SUMMARY"
if run_hook 207 "$WORK" "$SUMMARY" PATH="$FINALBIN:$PATH" >/dev/null 2>&1; then
  fail "blocked finalization unexpectedly passed"
fi
FM="$(find "$ARCHIVE/owner/repo/207/attempt_1" -name manifest.failed.json -type f -print -quit)"
[[ -n "$FM" ]] || fail "finalization timeout did not leave a failure manifest"
[[ "$(jq -r '.failure_code' "$FM")" == "LOCAL_ARTIFACT_ARCHIVE_TIMEOUT" ]] || fail "wrong finalization-timeout failure code"
if find "$ARCHIVE/owner/repo/207" -name manifest.json -type f -print -quit | grep -q .; then
  fail "finalization timeout published a PASS manifest"
fi

# Hash/finalization failure after workspace publication must never leave both
# PASS and FAILED state. manifest.json is published only after hash succeeds.
HASHBIN="$TMP/hashbin"
mkdir -p "$HASHBIN"
cat > "$HASHBIN/sha256sum" <<'MOCK'
#!/usr/bin/env bash
exit 1
MOCK
chmod +x "$HASHBIN/sha256sum"
write_config 1 30
SUMMARY="$TMP/hash-failure-summary.md"
: > "$SUMMARY"
if run_hook 208 "$WORK" "$SUMMARY" PATH="$HASHBIN:$PATH" >/dev/null 2>&1; then
  fail "forced manifest hash failure unexpectedly passed"
fi
FM="$(find "$ARCHIVE/owner/repo/208/attempt_1" -name manifest.failed.json -type f -print -quit)"
[[ -n "$FM" ]] || fail "manifest hash failure did not leave FAILED state"
if find "$ARCHIVE/owner/repo/208" -name manifest.json -type f -print -quit | grep -q .; then
  fail "manifest hash failure left an authoritative PASS manifest"
fi
[[ "$(jq -r '.failure_code' "$FM")" == "LOCAL_ARTIFACT_ARCHIVE_FAILED" ]] || fail "wrong manifest-hash failure code"

# Publication/timeout race: the worker publishes a complete PASS archive and
# then remains alive past the configured deadline. The parent must validate the
# published state and preserve PASS rather than creating FAILED state.
write_config 1 1
SUMMARY="$TMP/post-publish-timeout-summary.md"
: > "$SUMMARY"
if ! run_hook 209 "$WORK" "$SUMMARY" GRT_TEST_DELAY_AFTER_PASS_PUBLICATION=5 >/dev/null 2>&1; then
  fail "post-publication timeout race did not preserve PASS"
fi
M="$(find "$ARCHIVE/owner/repo/209/attempt_1" -name manifest.json -type f -print -quit)"
[[ -n "$M" ]] || fail "post-publication timeout race lost PASS manifest"
JOBDIR="$(dirname "$M")"
[[ -d "$JOBDIR/workspace" ]] || fail "post-publication timeout race lost workspace"
[[ -f "$JOBDIR/manifest.sha256" ]] || fail "post-publication timeout race lost manifest hash"
( cd "$JOBDIR" && sha256sum -c manifest.sha256 >/dev/null ) || fail "post-publication timeout race left invalid manifest hash"
[[ "$(jq -r '.archive_status' "$M")" == "PASS" ]] || fail "post-publication timeout race manifest is not PASS"
[[ "$(jq -r '.run_id' "$M")" == "209" ]] || fail "post-publication timeout race manifest identity mismatch"
if find "$ARCHIVE/owner/repo/209" -name manifest.failed.json -type f -print -quit | grep -q .; then
  fail "post-publication timeout race created contradictory FAILED manifest"
fi
grep -q 'Archive status: PASS' "$SUMMARY" || fail "post-publication timeout race did not write PASS summary"

echo "PASS: archive completed-hook failure-path tests"
