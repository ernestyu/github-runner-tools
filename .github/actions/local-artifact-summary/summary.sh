#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG="/etc/github-runner-tools/archive.conf"
[[ -r "$CONFIG" ]] || { echo "::error::Local archive config is unavailable on this runner."; exit 1; }

ARCHIVE_ROOT=""
RETENTION_DAYS=""
MIN_FREE_PERCENT=""
COPY_TIMEOUT_SECONDS=""
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  key="${line%%=*}"; value="${line#*=}"
  case "$key" in
    ARCHIVE_ROOT) ARCHIVE_ROOT="$value" ;;
    RETENTION_DAYS) RETENTION_DAYS="$value" ;;
    MIN_FREE_PERCENT) MIN_FREE_PERCENT="$value" ;;
    COPY_TIMEOUT_SECONDS) COPY_TIMEOUT_SECONDS="$value" ;;
    *) echo "::error::Unknown archive config key: $key"; exit 1 ;;
  esac
done < "$CONFIG"

[[ -n "$ARCHIVE_ROOT" && "$ARCHIVE_ROOT" = /* && -d "$ARCHIVE_ROOT" ]] || { echo "::error::Archive root is unavailable."; exit 1; }
[[ -n "${GITHUB_REPOSITORY:-}" && -n "${GITHUB_RUN_ID:-}" && -n "${GITHUB_RUN_ATTEMPT:-}" ]] || { echo "::error::Required GitHub run identity is unavailable."; exit 1; }
[[ -n "${GITHUB_STEP_SUMMARY:-}" && -w "$GITHUB_STEP_SUMMARY" ]] || { echo "::error::GITHUB_STEP_SUMMARY is unavailable."; exit 1; }

owner="${GITHUB_REPOSITORY%%/*}"
repo="${GITHUB_REPOSITORY##*/}"
sanitize() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^[._-]+//; s/[._-]+$//'
}
owner_path="$(sanitize "$owner")"
repo_path="$(sanitize "$repo")"
RUN_ROOT="$ARCHIVE_ROOT/$owner_path/$repo_path/$GITHUB_RUN_ID/attempt_$GITHUB_RUN_ATTEMPT"
[[ -d "$RUN_ROOT" ]] || { echo "::error::No local archive attempt found: $RUN_ROOT"; exit 1; }

mapfile -t manifests < <(find "$RUN_ROOT" -mindepth 2 -maxdepth 2 -name manifest.json -type f -print | sort)
mapfile -t failed < <(find "$RUN_ROOT" -mindepth 2 -maxdepth 2 -name manifest.failed.json -type f -print | sort)
[[ ${#manifests[@]} -gt 0 || ${#failed[@]} -gt 0 ]] || { echo "::error::No finalized local artifact manifests found."; exit 1; }

jobs=0 files=0 bytes=0
status="PASS"
for m in "${manifests[@]}"; do
  s="$(jq -r '.archive_status // empty' "$m")"
  [[ "$s" == "PASS" ]] || status="FAILED"
  jobs=$((jobs+1))
  f="$(jq -r '.file_count // 0' "$m")"
  b="$(jq -r '.total_bytes // 0' "$m")"
  [[ "$f" =~ ^[0-9]+$ ]] && files=$((files+f))
  [[ "$b" =~ ^[0-9]+$ ]] && bytes=$((bytes+b))
done
(( ${#failed[@]} == 0 )) || status="FAILED"

archive_uri="archive://$owner_path/$repo_path/$GITHUB_RUN_ID/"
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

[[ "$status" == "PASS" ]] || { echo "::error::One or more local artifact archives failed."; exit 1; }
