#!/usr/bin/env bash
# github-runner-tools-managed-hook
set -Eeuo pipefail

LIB="/usr/local/lib/github-runner-tools/archive-common.sh"
CONFIG="/etc/github-runner-tools/archive.conf"
if [[ "${GRT_TEST_MODE:-0}" == "1" && "${GITHUB_ACTIONS:-}" != "true" ]]; then
  LIB="${GRT_TEST_LIB:-$LIB}"
  CONFIG="${GRT_TEST_CONFIG:-$CONFIG}"
fi

[[ -r "$LIB" ]] || {
  echo "LOCAL_ARTIFACT_ARCHIVE_FAILED: shared library not readable: $LIB" >&2
  exit 1
}
# shellcheck source=/dev/null
source "$LIB"

FAILURE_CODE="LOCAL_ARTIFACT_ARCHIVE_FAILED"
FAILURE_MESSAGE="archive hook failed"
JOB_DIR=""
JOB_KEY=""
STAGE=""
SUMMARY_WRITTEN=0

write_failed_summary() {
  local code="$1" message="$2"
  [[ -n "${GITHUB_STEP_SUMMARY:-}" && -w "${GITHUB_STEP_SUMMARY:-/nonexistent}" ]] || return 0
  {
    echo "## Local CI Artifact"
    echo
    echo "Repository: ${GITHUB_REPOSITORY:-unknown}"
    echo "Run ID: ${GITHUB_RUN_ID:-unknown}"
    echo "Attempt: ${GITHUB_RUN_ATTEMPT:-unknown}"
    echo "Commit: ${GITHUB_SHA:-unknown}"
    echo "Archive status: FAILED"
    echo "Failure: $code"
    echo
    echo "$message"
  } >> "$GITHUB_STEP_SUMMARY" || true
  SUMMARY_WRITTEN=1
}

write_failure_manifest() {
  local code="$1" message="$2" now tmp
  [[ -n "$JOB_DIR" && -d "$JOB_DIR" && -w "$JOB_DIR" ]] || return 0
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  tmp="$JOB_DIR/.manifest.failed.tmp.$$"
  jq -n     --arg schema "github-runner-tools/local-artifact-manifest/v1"     --arg status "FAILED"     --arg repository "${GITHUB_REPOSITORY:-}"     --arg run_id "${GITHUB_RUN_ID:-}"     --arg run_attempt "${GITHUB_RUN_ATTEMPT:-}"     --arg job "${GITHUB_JOB:-}"     --arg job_key "$JOB_KEY"     --arg workflow "${GITHUB_WORKFLOW:-}"     --arg git_sha "${GITHUB_SHA:-}"     --arg git_ref "${GITHUB_REF:-}"     --arg runner_name "${RUNNER_NAME:-}"     --arg failed_at_utc "$now"     --arg failure_code "$code"     --arg failure_message "$message"     '{schema:$schema,archive_status:$status,repository:$repository,run_id:$run_id,run_attempt:$run_attempt,job:$job,job_key:$job_key,workflow:$workflow,git_sha:$git_sha,git_ref:$git_ref,runner_name:$runner_name,failed_at_utc:$failed_at_utc,failure_code:$failure_code,failure_message:$failure_message}'     > "$tmp" 2>/dev/null || return 0
  mv -f -- "$tmp" "$JOB_DIR/manifest.failed.json" 2>/dev/null || true
}

fail_archive() {
  local code="$1" message="$2" failed_stage=""
  trap - ERR
  set +e
  FAILURE_CODE="$code"
  FAILURE_MESSAGE="$message"
  write_failure_manifest "$code" "$message"
  write_failed_summary "$code" "$message"
  if [[ -n "$STAGE" && -d "$STAGE" && -n "$JOB_DIR" && -d "$JOB_DIR" ]]; then
    failed_stage="$JOB_DIR/.workspace.failed.$(date -u +%Y%m%dT%H%M%S).$$"
    mv -- "$STAGE" "$failed_stage" 2>/dev/null || true
    STAGE=""
  fi
  echo "::error title=Local artifact archive failed::$code: $message" >&2
  echo "$code: $message" >&2
  exit 1
}

on_err() {
  local rc="$1" line="$2"
  fail_archive "$FAILURE_CODE" "$FAILURE_MESSAGE (exit=$rc line=$line)"
}
trap 'on_err "$?" "$LINENO"' ERR

for cmd in jq rsync find du sha256sum df timeout date awk sed tr wc mkdir mv rm sleep grep stat flock; do
  grt_require_command "$cmd" || fail_archive "LOCAL_ARTIFACT_ARCHIVE_FAILED" "missing required command: $cmd"
done

grt_load_archive_config "$CONFIG" || fail_archive "LOCAL_ARTIFACT_ARCHIVE_FAILED" "invalid or unreadable archive config"
[[ -d "$ARCHIVE_ROOT" && ! -L "$ARCHIVE_ROOT" ]] || fail_archive "LOCAL_ARTIFACT_ARCHIVE_FAILED" "archive root does not exist or is a symlink"
ARCHIVE_ROOT="$(grt_canonical_dir "$ARCHIVE_ROOT")" || fail_archive "LOCAL_ARTIFACT_ARCHIVE_FAILED" "cannot canonicalize archive root"
[[ -w "$ARCHIVE_ROOT" ]] || fail_archive "LOCAL_ARTIFACT_ARCHIVE_FAILED" "archive root is not writable"
grt_is_world_writable "$ARCHIVE_ROOT" || fail_archive "LOCAL_ARTIFACT_ARCHIVE_FAILED" "archive root is world-writable or its mode cannot be validated"

grt_parse_repository "${GITHUB_REPOSITORY:-}" || fail_archive "LOCAL_ARTIFACT_IDENTITY_INVALID" "invalid GITHUB_REPOSITORY"
grt_validate_run_identity || fail_archive "LOCAL_ARTIFACT_IDENTITY_INVALID" "invalid run/attempt/job identity"

for v in GITHUB_SHA GITHUB_REF GITHUB_WORKFLOW RUNNER_NAME; do
  [[ -n "${!v:-}" ]] || fail_archive "LOCAL_ARTIFACT_IDENTITY_INVALID" "missing $v"
done

# Claim a unique, safe archive target as soon as execution identity is known.
# This allows later workspace/disk failures to leave a failure manifest.
OWNER_ROOT="$(grt_ensure_child_dir "$ARCHIVE_ROOT" "$GRT_OWNER_PATH")" || \
  fail_archive "LOCAL_ARTIFACT_PATH_CONFLICT" "unsafe owner archive path"
REPO_ROOT="$(grt_ensure_child_dir "$OWNER_ROOT" "$GRT_REPO_PATH")" || \
  fail_archive "LOCAL_ARTIFACT_PATH_CONFLICT" "unsafe repository archive path"
RUN_ROOT="$(grt_ensure_child_dir "$REPO_ROOT" "$GITHUB_RUN_ID")" || \
  fail_archive "LOCAL_ARTIFACT_PATH_CONFLICT" "unsafe run archive path"
ATTEMPT_ROOT="$(grt_ensure_child_dir "$RUN_ROOT" "attempt_$GITHUB_RUN_ATTEMPT")" || \
  fail_archive "LOCAL_ARTIFACT_PATH_CONFLICT" "unsafe attempt archive path"

JOB_KEY="$GRT_JOB_SAFE"
JOB_DIR="$ATTEMPT_ROOT/$JOB_KEY"
if ! mkdir -- "$JOB_DIR" 2>/dev/null; then
  safe_runner="$(grt_sanitize_component "$RUNNER_NAME")" || safe_runner="runner"
  while :; do
    suffix="$(date -u +%Y%m%dT%H%M%S)-$(printf '%04x' "$((RANDOM & 65535))")"
    JOB_KEY="${GRT_JOB_SAFE}__${safe_runner}__${suffix}"
    JOB_DIR="$ATTEMPT_ROOT/$JOB_KEY"
    mkdir -- "$JOB_DIR" 2>/dev/null && break
    sleep 0.01
  done
fi
JOB_DIR="$(grt_canonical_dir "$JOB_DIR")" || fail_archive "LOCAL_ARTIFACT_PATH_CONFLICT" "cannot canonicalize job archive path"
grt_assert_beneath "$ARCHIVE_ROOT" "$JOB_DIR" || fail_archive "LOCAL_ARTIFACT_PATH_CONFLICT" "job path escaped archive root"

LOCK_FILE="$JOB_DIR/.archive.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || fail_archive "LOCAL_ARTIFACT_PATH_CONFLICT" "archive target is already locked by another hook execution"

[[ -n "${GITHUB_WORKSPACE:-}" ]] || fail_archive "LOCAL_ARTIFACT_WORKSPACE_INVALID" "missing GITHUB_WORKSPACE"
[[ "$GITHUB_WORKSPACE" = /* && -d "$GITHUB_WORKSPACE" && ! -L "$GITHUB_WORKSPACE" && -r "$GITHUB_WORKSPACE" ]] || \
  fail_archive "LOCAL_ARTIFACT_WORKSPACE_INVALID" "workspace missing, relative, symlinked, or unreadable"

grt_disk_stats "$ARCHIVE_ROOT" || fail_archive "LOCAL_ARTIFACT_ARCHIVE_FAILED" "cannot read archive filesystem capacity"
(( GRT_FS_FREE_PERCENT >= MIN_FREE_PERCENT )) || \
  fail_archive "LOCAL_ARTIFACT_DISK_GUARD_FAILED" "archive filesystem free space ${GRT_FS_FREE_PERCENT}% is below threshold ${MIN_FREE_PERCENT}%"

FAILURE_CODE="LOCAL_ARTIFACT_ARCHIVE_FAILED"
FAILURE_MESSAGE="workspace archive copy failed"
STAGE="$JOB_DIR/.workspace.tmp.$$.$RANDOM"
mkdir -- "$STAGE"

RSYNC_ARGS=(
  -a
  --safe-links
  --exclude='.git/'
  --exclude='node_modules/'
  --exclude='.venv/'
  --exclude='venv/'
  --exclude='__pycache__/'
  --exclude='.pytest_cache/'
)

if timeout --signal=TERM --kill-after=30 "${COPY_TIMEOUT_SECONDS}s"   rsync "${RSYNC_ARGS[@]}" -- "$GITHUB_WORKSPACE/" "$STAGE/"; then
  :
else
  copy_rc=$?
  if [[ "$copy_rc" -eq 124 || "$copy_rc" -eq 137 ]]; then
    fail_archive "LOCAL_ARTIFACT_ARCHIVE_TIMEOUT" "workspace archive copy exceeded ${COPY_TIMEOUT_SECONDS}s"
  fi
  fail_archive "LOCAL_ARTIFACT_ARCHIVE_FAILED" "workspace archive copy failed with exit code $copy_rc"
fi

FAILURE_MESSAGE="archive finalization failed"
mv -- "$STAGE" "$JOB_DIR/workspace"
STAGE=""

FILE_COUNT="$(find "$JOB_DIR/workspace" -type f -printf . | wc -c | tr -d ' ')"
SYMLINK_COUNT="$(find "$JOB_DIR/workspace" -type l -printf . | wc -c | tr -d ' ')"
TOTAL_BYTES="$(du -sb "$JOB_DIR/workspace" | awk '{print $1}')"
COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ARCHIVE_URI="$(grt_archive_uri_for "$GRT_OWNER_PATH" "$GRT_REPO_PATH" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$JOB_KEY")"
MANIFEST_TMP="$JOB_DIR/.manifest.tmp.$$"

jq -n   --arg schema "github-runner-tools/local-artifact-manifest/v1"   --arg status "PASS"   --arg repository "$GITHUB_REPOSITORY"   --arg owner_path "$GRT_OWNER_PATH"   --arg repo_path "$GRT_REPO_PATH"   --arg run_id "$GITHUB_RUN_ID"   --arg run_attempt "$GITHUB_RUN_ATTEMPT"   --arg job "$GITHUB_JOB"   --arg job_key "$JOB_KEY"   --arg workflow "$GITHUB_WORKFLOW"   --arg git_sha "$GITHUB_SHA"   --arg git_ref "$GITHUB_REF"   --arg runner_name "$RUNNER_NAME"   --arg workspace_source "$GITHUB_WORKSPACE"   --arg completed_at_utc "$COMPLETED_AT"   --arg archive_path "$JOB_DIR"   --arg archive_uri "$ARCHIVE_URI"   --argjson file_count "$FILE_COUNT"   --argjson total_bytes "$TOTAL_BYTES"   --argjson symlink_count "$SYMLINK_COUNT"   --argjson disk_free_percent_before "$GRT_FS_FREE_PERCENT"   --argjson disk_free_bytes_before "$GRT_FS_FREE_BYTES"   '{schema:$schema,archive_status:$status,repository:$repository,owner_path:$owner_path,repo_path:$repo_path,run_id:$run_id,run_attempt:$run_attempt,job:$job,job_key:$job_key,workflow:$workflow,git_sha:$git_sha,git_ref:$git_ref,runner_name:$runner_name,workspace_source:$workspace_source,completed_at_utc:$completed_at_utc,archive_path:$archive_path,archive_uri:$archive_uri,file_count:$file_count,total_bytes:$total_bytes,symlink_count:$symlink_count,disk_free_percent_before:$disk_free_percent_before,disk_free_bytes_before:$disk_free_bytes_before,exclude_policy_version:"v1"}'   > "$MANIFEST_TMP"
mv -- "$MANIFEST_TMP" "$JOB_DIR/manifest.json"
sha256sum "$JOB_DIR/manifest.json" | awk '{print $1"  manifest.json"}' > "$JOB_DIR/manifest.sha256"
rm -f -- "$JOB_DIR/manifest.failed.json"

if [[ -n "${GITHUB_STEP_SUMMARY:-}" && -w "${GITHUB_STEP_SUMMARY:-/nonexistent}" ]]; then
  {
    echo "## Local CI Artifact"
    echo
    echo "Repository: $GITHUB_REPOSITORY"
    echo "Run ID: $GITHUB_RUN_ID"
    echo "Attempt: $GITHUB_RUN_ATTEMPT"
    echo "Commit: $GITHUB_SHA"
    echo "Runner: $RUNNER_NAME"
    echo "Archive status: PASS"
    echo "Files: $FILE_COUNT"
    echo "Size: $TOTAL_BYTES bytes"
    echo
    echo "Local archive:"
    echo "$ARCHIVE_URI"
  } >> "$GITHUB_STEP_SUMMARY"
  SUMMARY_WRITTEN=1
else
  echo "LOCAL_ARTIFACT_SUMMARY_UNAVAILABLE: GITHUB_STEP_SUMMARY is absent or not writable" >&2
fi

rm -f -- "$LOCK_FILE"
flock -u 9 || true
exec 9>&-
trap - ERR
echo "LOCAL_ARTIFACT_ARCHIVE_PASS: $ARCHIVE_URI files=$FILE_COUNT bytes=$TOTAL_BYTES summary_written=$SUMMARY_WRITTEN"
