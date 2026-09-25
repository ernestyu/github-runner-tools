#!/usr/bin/env bash
set -Eeuo pipefail

LIB="/usr/local/lib/github-runner-tools/archive-common.sh"
CONFIG="/etc/github-runner-tools/archive.conf"
if [[ "${GRT_TEST_MODE:-0}" == "1" && "${GITHUB_ACTIONS:-}" != "true" ]]; then
  LIB="${GRT_TEST_LIB:-$LIB}"
  CONFIG="${GRT_TEST_CONFIG:-$CONFIG}"
fi

[[ -r "$LIB" ]] || { echo "::error::Local archive library is unavailable on this runner."; exit 1; }
# shellcheck source=/dev/null
source "$LIB"

for cmd in jq find sort; do
  grt_require_command "$cmd" || exit 1
done

grt_load_archive_config "$CONFIG" || { echo "::error::Local archive config is unavailable or invalid."; exit 1; }
[[ -d "$ARCHIVE_ROOT" && ! -L "$ARCHIVE_ROOT" ]] || { echo "::error::Archive root is unavailable."; exit 1; }
ARCHIVE_ROOT="$(grt_canonical_dir "$ARCHIVE_ROOT")"

[[ -n "${GITHUB_REPOSITORY:-}" ]] || { echo "::error::GITHUB_REPOSITORY is unavailable."; exit 1; }
[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]] || { echo "::error::GITHUB_RUN_ID is invalid."; exit 1; }
[[ "${GITHUB_RUN_ATTEMPT:-}" =~ ^[0-9]+$ ]] || { echo "::error::GITHUB_RUN_ATTEMPT is invalid."; exit 1; }
[[ -n "${GITHUB_STEP_SUMMARY:-}" && -w "$GITHUB_STEP_SUMMARY" ]] || { echo "::error::GITHUB_STEP_SUMMARY is unavailable."; exit 1; }

grt_parse_repository "$GITHUB_REPOSITORY" || { echo "::error::Repository identity is invalid."; exit 1; }

RUN_ROOT="$ARCHIVE_ROOT/$GRT_OWNER_PATH/$GRT_REPO_PATH/$GITHUB_RUN_ID/attempt_$GITHUB_RUN_ATTEMPT"
[[ -d "$RUN_ROOT" && ! -L "$RUN_ROOT" ]] || { echo "::error::No local archive attempt found: $RUN_ROOT"; exit 1; }
RUN_ROOT="$(grt_canonical_dir "$RUN_ROOT")"
grt_assert_beneath "$ARCHIVE_ROOT" "$RUN_ROOT" || { echo "::error::Local archive attempt escapes archive root."; exit 1; }

mapfile -t manifests < <(find "$RUN_ROOT" -mindepth 2 -maxdepth 2 -name manifest.json -type f -print | sort)
mapfile -t failed < <(find "$RUN_ROOT" -mindepth 2 -maxdepth 2 -name manifest.failed.json -type f -print | sort)
[[ ${#manifests[@]} -gt 0 || ${#failed[@]} -gt 0 ]] || { echo "::error::No finalized local artifact manifests found."; exit 1; }

jobs=0
files=0
bytes=0
status="PASS"

for m in "${manifests[@]}"; do
  repository="$(jq -r '.repository // empty' "$m")"
  run_id="$(jq -r '.run_id // empty' "$m")"
  attempt="$(jq -r '.run_attempt // empty' "$m")"
  manifest_status="$(jq -r '.archive_status // empty' "$m")"
  [[ "$repository" == "$GITHUB_REPOSITORY" && "$run_id" == "$GITHUB_RUN_ID" && "$attempt" == "$GITHUB_RUN_ATTEMPT" ]] || {
    echo "::error::Manifest identity mismatch: $m"
    exit 1
  }
  [[ "$manifest_status" == "PASS" ]] || status="FAILED"

  jobs=$((jobs + 1))
  f="$(jq -r '.file_count // 0' "$m")"
  b="$(jq -r '.total_bytes // 0' "$m")"
  [[ "$f" =~ ^[0-9]+$ ]] && files=$((files + f))
  [[ "$b" =~ ^[0-9]+$ ]] && bytes=$((bytes + b))
done

if (( ${#failed[@]} > 0 )); then
  status="FAILED"
fi

archive_uri="$(grt_archive_uri_for "$GRT_OWNER_PATH" "$GRT_REPO_PATH" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT")"
{
  echo "## Local CI Artifact"
  echo
  echo "Repository: $GITHUB_REPOSITORY"
  echo "Run ID: $GITHUB_RUN_ID"
  echo "Attempt: $GITHUB_RUN_ATTEMPT"
  echo "Commit: ${GITHUB_SHA:-unknown}"
  echo "Archive status: $status"
  echo
  echo "Jobs archived: $jobs"
  echo "Files: $files"
  echo "Size: $bytes bytes"
  echo
  echo "Local archive:"
  echo "$archive_uri"
} >> "$GITHUB_STEP_SUMMARY"

[[ "$status" == "PASS" ]] || {
  echo "::error::One or more local artifact archives failed."
  exit 1
}
