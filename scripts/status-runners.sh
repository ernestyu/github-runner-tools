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

RUNNER_USER="$(id -un)"
USER_HOME="$(resolve_home "$RUNNER_USER")" || die "Could not resolve home for $RUNNER_USER"
RUNNER_BASE_DIR="${RUNNER_BASE_DIR:-$USER_HOME}"
[[ -d "$RUNNER_BASE_DIR" ]] || die "Runner base directory does not exist: $RUNNER_BASE_DIR"
RUNNER_BASE_DIR="$(cd -- "$RUNNER_BASE_DIR" && pwd -P)"

printf 'Runner user : %s\n' "$RUNNER_USER"
printf 'Runner base : %s\n\n' "$RUNNER_BASE_DIR"

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
