#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOOL_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DEFAULT_BASE_DIR="$(cd -- "$TOOL_ROOT/.." && pwd)"
RUNNER_BASE_DIR="${RUNNER_BASE_DIR:-$DEFAULT_BASE_DIR}"

printf 'Tool root   : %s\n' "$TOOL_ROOT"
printf 'Runner base : %s\n\n' "$RUNNER_BASE_DIR"

shopt -s nullglob
RUNNER_DIRS=("$RUNNER_BASE_DIR"/actions-runner-*)
shopt -u nullglob

if [[ ${#RUNNER_DIRS[@]} -eq 0 ]]; then
  echo "No actions-runner-* directories found."
  exit 0
fi

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
