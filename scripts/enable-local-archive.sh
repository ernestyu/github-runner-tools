#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/archive-common.sh
source "$ROOT/scripts/lib/archive-common.sh"

HOOK_PATH="/usr/local/lib/github-runner-tools/hooks/archive-job-completed.sh"
CONFIG_PATH="/etc/github-runner-tools/archive.conf"
if [[ "${GRT_TEST_MODE:-0}" == "1" && "${GITHUB_ACTIONS:-}" != "true" ]]; then
  HOOK_PATH="${GRT_TEST_HOOK_PATH:-$HOOK_PATH}"
  CONFIG_PATH="${GRT_TEST_CONFIG:-$CONFIG_PATH}"
fi
MODE="dry-run"

usage() {
  cat <<'USAGE'
Usage:
  enable-local-archive.sh [--dry-run|--apply]

Default: --dry-run

Environment:
  RUNNER_BASE_DIR   Base directory containing actions-runner-* directories.
USAGE
}

die() { echo "ERROR: $*" >&2; exit 1; }

if [[ ${EUID} -eq 0 ]]; then die "Run as the normal runner owner, not root."; fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) MODE="dry-run"; shift ;;
    --apply) MODE="apply"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

RUNNER_USER="$(id -un)"
if command -v getent >/dev/null 2>&1; then
  USER_HOME="$(getent passwd "$RUNNER_USER" | awk -F: 'NR==1 {print $6}')"
else
  USER_HOME="${HOME:-}"
fi
[[ -n "$USER_HOME" && -d "$USER_HOME" ]] || die "Could not resolve runner home."
RUNNER_BASE_DIR="${RUNNER_BASE_DIR:-$USER_HOME}"
RUNNER_BASE_DIR="$(grt_canonical_dir "$RUNNER_BASE_DIR")" || die "Invalid RUNNER_BASE_DIR."

for cmd in jq systemctl sudo find awk stat tr grep; do grt_require_command "$cmd" || exit 1; done
grt_load_archive_config "$CONFIG_PATH" || die "Archive platform config is missing or invalid. Run setup-local-archive.sh first."
[[ -x "$HOOK_PATH" ]] || die "Shared archive hook is missing or not executable: $HOOK_PATH"
[[ -d "$ARCHIVE_ROOT" && ! -L "$ARCHIVE_ROOT" && -w "$ARCHIVE_ROOT" ]] || die "Archive root is unavailable to $RUNNER_USER."
grt_is_world_writable "$ARCHIVE_ROOT" || die "Archive root must not be world-writable."
if [[ "${GRT_TEST_MODE:-0}" != "1" ]]; then
  [[ "$(stat -c '%u' "$HOOK_PATH")" == "0" && ! -w "$HOOK_PATH" ]] || die "Shared hook must be root-owned and not writable by $RUNNER_USER."
  [[ "$(stat -c '%u' "$CONFIG_PATH")" == "0" && ! -w "$CONFIG_PATH" ]] || die "Archive config must be root-owned and not writable by $RUNNER_USER."
fi

shopt -s nullglob
dirs=("$RUNNER_BASE_DIR"/actions-runner-*)
shopt -u nullglob
[[ ${#dirs[@]} -gt 0 ]] || { echo "No actions-runner-* directories found."; exit 0; }

failures=0
for dir in "${dirs[@]}"; do
  echo "============================================================"
  echo "Candidate: $dir"
  if [[ ! -f "$dir/.runner" || ! -f "$dir/.service" ]]; then
    echo "SKIP: missing .runner or .service metadata"
    continue
  fi
  DIR_OWNER="$(stat -c '%U' "$dir" 2>/dev/null || true)"
  if [[ "$DIR_OWNER" != "$RUNNER_USER" ]]; then
    echo "ERROR: runner directory owner is $DIR_OWNER, expected $RUNNER_USER" >&2
    failures=$((failures+1))
    continue
  fi

  REPO_URL="$(jq -r '.gitHubUrl // empty' "$dir/.runner" 2>/dev/null || true)"
  RUNNER_NAME="$(jq -r '.agentName // empty' "$dir/.runner" 2>/dev/null || true)"
  SERVICE_NAME="$(tr -d '\r\n' < "$dir/.service")"
  if [[ -z "$REPO_URL" || -z "$RUNNER_NAME" || -z "$SERVICE_NAME" || "$SERVICE_NAME" != actions.runner.* ]]; then
    echo "SKIP: runner metadata is invalid"
    continue
  fi
  if ! grt_repository_from_github_url "$REPO_URL"; then
    echo "SKIP: runner repository URL is not a valid GitHub repository identity: $REPO_URL"
    continue
  fi

  ENV_FILE="$dir/.env"
  current=""
  if current="$(grt_read_runner_env_value "$ENV_FILE" ACTIONS_RUNNER_HOOK_JOB_COMPLETED 2>/dev/null)"; then
    if [[ "$current" != "$HOOK_PATH" ]]; then
      echo "ERROR: conflicting completed hook: $current" >&2
      failures=$((failures+1))
      continue
    fi
    echo "Hook: already configured"
  else
    echo "Hook: would set ACTIONS_RUNNER_HOOK_JOB_COMPLETED=$HOOK_PATH"
  fi

  echo "Repository: $REPO_URL"
  echo "Runner    : $RUNNER_NAME"
  echo "Service   : $SERVICE_NAME"

  if [[ "$MODE" == "apply" ]]; then
    if ! grt_set_runner_env_value "$ENV_FILE" ACTIONS_RUNNER_HOOK_JOB_COMPLETED "$HOOK_PATH"; then
      echo "ERROR: failed to update $ENV_FILE" >&2
      failures=$((failures+1))
      continue
    fi
    if ! sudo systemctl restart "$SERVICE_NAME"; then
      echo "ERROR: failed to restart $SERVICE_NAME" >&2
      failures=$((failures+1))
      continue
    fi
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
      echo "ERROR: service is not active after restart: $SERVICE_NAME" >&2
      failures=$((failures+1))
      continue
    fi
    echo "APPLIED: hook configured and service active"
  else
    echo "DRY-RUN: no changes made"
  fi
done

(( failures == 0 )) || die "$failures runner(s) failed migration checks."
echo "Existing-runner archive migration $MODE completed."
