#!/usr/bin/env bash
set -Eeuo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
resolve_home() {
  local user="$1" home=""
  if command -v getent >/dev/null 2>&1; then home="$(getent passwd "$user" | awk -F: 'NR==1 {print $6}')"; fi
  [[ -n "$home" ]] || home="${HOME:-}"
  [[ -n "$home" && "$home" = /* ]] || return 1
  printf '%s' "$home"
}

canonicalize_existing_dir() {
  local dir="$1"
  [[ -d "$dir" ]] || return 1
  (cd -- "$dir" && pwd -P)
}

RUNNER_USER="$(id -un)"
USER_HOME="$(resolve_home "$RUNNER_USER")" || die "Could not resolve home for $RUNNER_USER"
RUNNER_BASE_DIR="${RUNNER_BASE_DIR:-$USER_HOME}"
[[ -d "$RUNNER_BASE_DIR" ]] || die "Runner base directory does not exist: $RUNNER_BASE_DIR"
RUNNER_BASE_DIR="$(canonicalize_existing_dir "$RUNNER_BASE_DIR")" || die "Could not canonicalize RUNNER_BASE_DIR: $RUNNER_BASE_DIR"

printf 'Runner user : %s\n' "$RUNNER_USER"
printf 'Runner base : %s\n' "$RUNNER_BASE_DIR"

ARCHIVE_LIB="/usr/local/lib/github-runner-tools/archive-common.sh"
ARCHIVE_CONFIG="/etc/github-runner-tools/archive.conf"
ARCHIVE_HOOK="/usr/local/lib/github-runner-tools/hooks/archive-job-completed.sh"
echo
echo "Local artifact archive:"
echo "  config: $ARCHIVE_CONFIG"
echo "  hook  : $ARCHIVE_HOOK"
if [[ -r "$ARCHIVE_LIB" && -r "$ARCHIVE_CONFIG" ]]; then
  # shellcheck source=/dev/null
  source "$ARCHIVE_LIB"
  if grt_load_archive_config "$ARCHIVE_CONFIG" && [[ -d "$ARCHIVE_ROOT" ]]; then
    writable="no"; [[ -w "$ARCHIVE_ROOT" ]] && writable="yes"
    if grt_disk_stats "$ARCHIVE_ROOT"; then
      echo "  root                 : $ARCHIVE_ROOT"
      world_writable="yes"; grt_is_world_writable "$ARCHIVE_ROOT" && world_writable="no"
      echo "  root writable        : $writable"
      echo "  root world-writable  : $world_writable"
      echo "  filesystem total     : $GRT_FS_TOTAL_BYTES bytes"
      echo "  filesystem used      : $GRT_FS_USED_BYTES bytes"
      echo "  filesystem free      : $GRT_FS_FREE_BYTES bytes"
      echo "  free percentage      : $GRT_FS_FREE_PERCENT%"
      echo "  retention days       : $RETENTION_DAYS"
      echo "  disk guard threshold : $MIN_FREE_PERCENT%"
      echo "  copy timeout         : $COPY_TIMEOUT_SECONDS seconds"
      echo "  hook executable      : $([[ -x "$ARCHIVE_HOOK" ]] && echo yes || echo no)"
    else
      echo "  status: configured, but filesystem statistics failed"
    fi
  else
    echo "  status: config invalid or archive root missing"
  fi
else
  echo "  status: not configured"
fi
echo

shopt -s nullglob
RUNNER_DIRS=("$RUNNER_BASE_DIR"/actions-runner-*)
shopt -u nullglob
if [[ ${#RUNNER_DIRS[@]} -eq 0 ]]; then echo "No actions-runner-* directories found."; exit 0; fi

for dir in "${RUNNER_DIRS[@]}"; do
  echo "============================================================"
  echo "Runner directory: $dir"
  if [[ -f "$dir/.runner" ]]; then
    if command -v jq >/dev/null 2>&1; then
      NAME="$(jq -r '.agentName // "unknown"' "$dir/.runner" 2>/dev/null || echo unknown)"
      URL="$(jq -r '.gitHubUrl // .serverUrl // "unknown"' "$dir/.runner" 2>/dev/null || echo unknown)"
      echo "Runner name     : $NAME"
      echo "GitHub URL      : $URL"
    else
      echo "Configured      : yes"
    fi
  else
    echo "Configured      : no (.runner not found)"
  fi
  if [[ -x "$dir/svc.sh" ]]; then
    echo "Service status:"
    (cd "$dir" && sudo ./svc.sh status) || true
  else
    echo "Service status  : svc.sh not found"
  fi
  echo
done

echo "============================================================"
echo "Systemd runner services:"
systemctl --type=service --all 2>/dev/null | grep actions.runner || true
