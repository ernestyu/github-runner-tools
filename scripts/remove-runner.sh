#!/usr/bin/env bash
set -Eeuo pipefail

MAX_LOCAL_ID_LENGTH=64
HASH_LENGTH=8
TOKEN=""

usage() {
  cat <<'USAGE'
Usage:
  remove-runner.sh [--base-dir PATH] OWNER/REPO

Environment variable:
  RUNNER_BASE_DIR
USAGE
}

die() { echo "ERROR: $*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }
cleanup() { unset TOKEN || true; }
trap cleanup EXIT

sanitize_component() {
  local value="$1" out
  out="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^-+//; s/-+$//')"
  [[ -n "$out" ]] || return 1
  printf '%s' "$out"
}

make_local_id() {
  local owner="$1" repo="$2" safe_owner safe_repo direct normalized hash
  local available owner_len repo_len base_each
  safe_owner="$(sanitize_component "$owner")" || return 1
  safe_repo="$(sanitize_component "$repo")" || return 1
  direct="${safe_owner}--${safe_repo}"
  if (( ${#direct} <= MAX_LOCAL_ID_LENGTH )); then printf '%s' "$direct"; return 0; fi
  normalized="$(printf '%s/%s' "$owner" "$repo" | tr '[:upper:]' '[:lower:]')"
  hash="$(printf '%s' "$normalized" | sha256sum | awk '{print $1}' | cut -c1-${HASH_LENGTH})"
  available=$((MAX_LOCAL_ID_LENGTH - 2 - 2 - HASH_LENGTH))
  base_each=$((available / 2))
  owner_len=${#safe_owner}; repo_len=${#safe_repo}
  if (( owner_len < base_each )); then
    repo_len=$((available-owner_len)); (( repo_len > ${#safe_repo} )) && repo_len=${#safe_repo}; owner_len=$((available-repo_len))
  elif (( repo_len < base_each )); then
    owner_len=$((available-repo_len)); (( owner_len > ${#safe_owner} )) && owner_len=${#safe_owner}; repo_len=$((available-owner_len))
  else
    owner_len=$base_each; repo_len=$((available-owner_len))
  fi
  (( owner_len >= 1 && repo_len >= 1 )) || return 1
  printf '%s--%s--%s' "${safe_owner:0:owner_len}" "${safe_repo:0:repo_len}" "$hash"
}

resolve_home() {
  local user="$1" home=""
  if command -v getent >/dev/null 2>&1; then home="$(getent passwd "$user" | awk -F: 'NR==1 {print $6}')"; fi
  [[ -n "$home" ]] || home="${HOME:-}"
  [[ -n "$home" && "$home" = /* ]] || return 1
  printf '%s' "$home"
}

normalize_repo_url() {
  local value="$1"
  value="${value%/}"
  value="${value%.git}"
  printf '%s' "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
}

metadata_repo_url() {
  local file="$1"
  jq -r '.gitHubUrl // empty' "$file" 2>/dev/null || true
}

read_tty_line() {
  local prompt="$1" secret="${2:-0}" value
  [[ -r /dev/tty && -w /dev/tty ]] || die "Interactive input requires a TTY."
  if [[ "$secret" == "1" ]]; then
    IFS= read -r -s -p "$prompt" value < /dev/tty
    printf '\n' > /dev/tty
  else
    IFS= read -r -p "$prompt" value < /dev/tty
  fi
  printf '%s' "$value"
}

if [[ ${EUID} -eq 0 ]]; then die "Do not run this script as root. Run it as the runner owner."; fi
for cmd in jq sudo id awk tr sed sha256sum cut find; do require_command "$cmd"; done

CLI_BASE_DIR=""; POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir) [[ $# -ge 2 ]] || die "--base-dir requires a value"; CLI_BASE_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    -*) die "Unknown option: $1" ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
set -- "${POSITIONAL[@]}"
[[ $# -eq 1 ]] || { usage; exit 1; }
REPO="$1"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "Repository must be in OWNER/REPO form."
OWNER="${REPO%%/*}"; REPO_NAME="${REPO##*/}"
SAFE_REPO="$(sanitize_component "$REPO_NAME")" || die "Could not derive safe repository name."
LOCAL_ID="$(make_local_id "$OWNER" "$REPO_NAME")" || die "Could not derive local identity."
RUNNER_USER="$(id -un)"; USER_HOME="$(resolve_home "$RUNNER_USER")" || die "Could not resolve home for $RUNNER_USER"
RUNNER_BASE_DIR="${CLI_BASE_DIR:-${RUNNER_BASE_DIR:-$USER_HOME}}"
[[ -d "$RUNNER_BASE_DIR" && -x "$RUNNER_BASE_DIR" && -r "$RUNNER_BASE_DIR" && -w "$RUNNER_BASE_DIR" ]] || die "RUNNER_BASE_DIR is not accessible and writable: $RUNNER_BASE_DIR"

NEW_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"
LEGACY_DIR="$RUNNER_BASE_DIR/actions-runner-$SAFE_REPO"
RUNNER_DIR=""
MODE=""

if [[ -d "$NEW_DIR" ]]; then
  RUNNER_DIR="$NEW_DIR"; MODE="new"
fi
if [[ -d "$LEGACY_DIR" ]]; then
  [[ -f "$LEGACY_DIR/.runner" ]] || die "Legacy runner candidate is ambiguous because .runner metadata is missing: $LEGACY_DIR"
  META_URL="$(metadata_repo_url "$LEGACY_DIR/.runner")"
  [[ -n "$META_URL" ]] || die "Legacy runner candidate is ambiguous because repository metadata cannot be verified: $LEGACY_DIR"
  EXPECTED_URL="https://github.com/$REPO"
  if [[ "$(normalize_repo_url "$META_URL")" == "$(normalize_repo_url "$EXPECTED_URL")" ]]; then
    if [[ -n "$RUNNER_DIR" && "$RUNNER_DIR" != "$LEGACY_DIR" ]]; then
      die "Both new and verified legacy runner directories exist. Refusing to guess which one to remove."
    fi
    RUNNER_DIR="$LEGACY_DIR"; MODE="legacy"
  elif [[ -z "$RUNNER_DIR" ]]; then
    die "Legacy runner directory belongs to a different repository: $META_URL"
  fi
fi

[[ -n "$RUNNER_DIR" ]] || die "Runner directory not found for $REPO"
[[ -f "$RUNNER_DIR/.runner" ]] || die "Configured runner metadata not found: $RUNNER_DIR/.runner"
[[ -x "$RUNNER_DIR/config.sh" && -x "$RUNNER_DIR/svc.sh" ]] || die "Runner management scripts are missing from: $RUNNER_DIR"

cat <<INFO
Repository : $REPO
Runner dir : $RUNNER_DIR
Mode       : $MODE

Before continuing, open:
  https://github.com/$REPO/settings/actions/runners

Select the runner, choose Remove, and copy the temporary removal token.
INFO

TOKEN="$(read_tty_line "Paste GitHub removal token: " 1)"
[[ -n "$TOKEN" ]] || die "Removal token cannot be empty."
CONFIRM="$(read_tty_line "Type REMOVE to unregister and delete this runner: ")"
[[ "$CONFIRM" == "REMOVE" ]] || die "Cancelled."

cd "$RUNNER_DIR"
echo "==> Stopping service..."
if ! sudo ./svc.sh stop; then
  echo "WARNING: Service stop failed; continuing to service uninstall in case it is already stopped." >&2
fi

echo "==> Uninstalling service..."
sudo ./svc.sh uninstall || die "Systemd service uninstall failed. Local runner directory will not be deleted."

echo "==> Removing runner registration from GitHub..."
./config.sh remove --token "$TOKEN"
unset TOKEN

cd "$RUNNER_BASE_DIR"
case "$RUNNER_DIR" in "$RUNNER_BASE_DIR"/actions-runner-*) ;; *) die "Safety check failed; refusing to delete unexpected path: $RUNNER_DIR" ;; esac

echo "==> Deleting local runner directory..."
rm -rf -- "$RUNNER_DIR"

echo
cat <<DONE
Runner removed successfully.
Repository: https://github.com/$REPO
Deleted:    $RUNNER_DIR
DONE
