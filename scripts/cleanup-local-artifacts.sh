#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/lib/archive-common.sh"

CONFIG="/etc/github-runner-tools/archive.conf"
if [[ "${GRT_TEST_MODE:-0}" == "1" && "${GITHUB_ACTIONS:-}" != "true" ]]; then
  CONFIG="${GRT_TEST_CONFIG:-$CONFIG}"
fi
MODE="dry-run"
VERBOSE=0

usage() {
  cat <<'USAGE'
Usage:
  cleanup-local-artifacts.sh [--dry-run|--apply] [--verbose]

Default: --dry-run
USAGE
}
die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) MODE="dry-run"; shift ;;
    --apply) MODE="apply"; shift ;;
    --verbose) VERBOSE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

grt_load_archive_config "$CONFIG" || die "Archive config missing or invalid."
[[ -d "$ARCHIVE_ROOT" && ! -L "$ARCHIVE_ROOT" ]] || die "Archive root does not exist or is a symlink."
ARCHIVE_ROOT="$(grt_canonical_dir "$ARCHIVE_ROOT")"
grt_is_world_writable "$ARCHIVE_ROOT" || die "Archive root must not be world-writable."
NOW="$(date -u +%s)"
CUTOFF=$((NOW - RETENTION_DAYS * 86400))

is_active_or_ambiguous() {
  local run="$1"
  find "$run" \( \( -type d -a \( -name '.workspace.tmp.*' -o -name '.workspace.failed.*' \) \) -o -name '.archive.lock' \) -print -quit | grep -q . && return 0
  find "$run" -name 'manifest.failed.json' -print -quit | grep -q . && return 0
  return 1
}

newest_completion_epoch() {
  local run="$1" newest=0 ts epoch file
  while IFS= read -r file; do
    ts="$(jq -r '.completed_at_utc // .downloaded_at_utc // empty' "$file" 2>/dev/null || true)"
    [[ -n "$ts" ]] || continue
    epoch="$(grt_iso_to_epoch "$ts" || true)"
    [[ -n "$epoch" ]] || continue
    (( epoch > newest )) && newest="$epoch"
  done < <(find "$run" -type f \( -name manifest.json -o -name artifact-manifest.json \) -print)
  (( newest > 0 )) || return 1
  printf '%s' "$newest"
}

deleted=0
eligible=0
while IFS= read -r -d '' run; do
  grt_assert_beneath "$ARCHIVE_ROOT" "$run" || { echo "SKIP unsafe path: $run"; continue; }
  if [[ -e "$run/.keep" ]]; then
    (( VERBOSE )) && echo "SKIP kept: $run"
    continue
  fi
  if is_active_or_ambiguous "$run"; then
    (( VERBOSE )) && echo "SKIP incomplete/active: $run"
    continue
  fi
  epoch="$(newest_completion_epoch "$run" || true)"
  if [[ -z "$epoch" ]]; then
    (( VERBOSE )) && echo "SKIP no finalized manifest: $run"
    continue
  fi
  if (( epoch >= CUTOFF )); then
    (( VERBOSE )) && echo "SKIP recent: $run"
    continue
  fi
  eligible=$((eligible+1))
  if [[ "$MODE" == "apply" ]]; then
    echo "DELETE: $run"
    rm -rf --one-file-system -- "$run"
    deleted=$((deleted+1))
  else
    echo "WOULD DELETE: $run"
  fi
done < <(find "$ARCHIVE_ROOT" -mindepth 3 -maxdepth 3 -type d -regextype posix-extended -regex '.*/[0-9]+' -print0)

echo "Cleanup mode=$MODE retention_days=$RETENTION_DAYS eligible=$eligible deleted=$deleted"
