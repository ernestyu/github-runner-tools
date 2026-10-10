#!/usr/bin/env bash
set -Eeuo pipefail

MAX_LOCAL_ID_LENGTH=64
HASH_LENGTH=8
CREATED_RUNNER_DIR=0
REGISTRATION_COMPLETE=0
CREATE_REQUEST_STARTED=0
TOKEN=""
RELEASE_JSON=""
ARCHIVE_PATH=""
LOCAL_ARCHIVE_HOOK="/usr/local/lib/github-runner-tools/hooks/archive-job-completed.sh"
LOCAL_ARCHIVE_LIB="/usr/local/lib/github-runner-tools/archive-common.sh"
LOCAL_ARCHIVE_CONFIG="/etc/github-runner-tools/archive.conf"
MUTATION_LOCK_DIR="/run/lock/github-runner-tools"
MUTATION_LOCK_FILE="$MUTATION_LOCK_DIR/mutation.lock"
WEB_MODE=0
TOKEN_FD=""
PRIVILEGED_FD=""
LOCK_ALREADY_HELD=0

usage() {
  cat <<'USAGE'
Usage:
  register-runner.sh [options] OWNER/REPO

Options:
  --base-dir PATH          Override runner base directory
  --runner-name NAME       Override runner name
  --labels LIST            Override custom labels (comma-separated)
  --runner-version VERSION Pin GitHub Actions Runner version (2.328.0 or v2.328.0)
  --clean-incomplete       Remove an existing non-empty, unconfigured target directory
  -h, --help               Show this help

Environment variables:
  RUNNER_BASE_DIR
  RUNNER_NAME
  RUNNER_LABELS
  RUNNER_VERSION

Cross-user provisioning is intentionally unsupported in v1.
USAGE
}

die() { echo "ERROR: $*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

cleanup() {
  unset TOKEN || true
  [[ -n "${RELEASE_JSON:-}" ]] && rm -f -- "$RELEASE_JSON" || true
  [[ -n "${ARCHIVE_PATH:-}" ]] && rm -f -- "$ARCHIVE_PATH" || true
  if [[ "$CREATED_RUNNER_DIR" -eq 1 && "$REGISTRATION_COMPLETE" -eq 0 && "$CREATE_REQUEST_STARTED" -eq 0 && -n "${RUNNER_DIR:-}" ]]; then
    if [[ -d "$RUNNER_DIR" && ! -e "$RUNNER_DIR/.runner" ]]; then rm -rf -- "$RUNNER_DIR" || true; fi
  fi
}

sanitize_component() {
  local value="$1" out
  out="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^-+//; s/-+$//')"
  [[ -n "$out" ]] || return 1
  printf '%s' "$out"
}

make_local_id() {
  local owner="$1" repo="$2" safe_owner safe_repo direct normalized hash
  local sep='--' hash_sep='--' available owner_len repo_len base_each
  safe_owner="$(sanitize_component "$owner")" || return 1
  safe_repo="$(sanitize_component "$repo")" || return 1
  direct="${safe_owner}${sep}${safe_repo}"
  if (( ${#direct} <= MAX_LOCAL_ID_LENGTH )); then printf '%s' "$direct"; return 0; fi
  normalized="$(printf '%s/%s' "$owner" "$repo" | tr '[:upper:]' '[:lower:]')"
  hash="$(printf '%s' "$normalized" | sha256sum | awk '{print $1}' | cut -c1-${HASH_LENGTH})"
  available=$((MAX_LOCAL_ID_LENGTH - ${#sep} - ${#hash_sep} - HASH_LENGTH))
  (( available >= 2 )) || return 1
  base_each=$((available / 2))
  owner_len=${#safe_owner}; repo_len=${#safe_repo}
  if (( owner_len < base_each )); then
    repo_len=$((available - owner_len)); (( repo_len > ${#safe_repo} )) && repo_len=${#safe_repo}; owner_len=$((available - repo_len))
  elif (( repo_len < base_each )); then
    owner_len=$((available - repo_len)); (( owner_len > ${#safe_owner} )) && owner_len=${#safe_owner}; repo_len=$((available - owner_len))
  else
    owner_len=$base_each; repo_len=$((available - owner_len))
  fi
  (( owner_len >= 1 && repo_len >= 1 )) || return 1
  printf '%s%s%s%s%s' "${safe_owner:0:owner_len}" "$sep" "${safe_repo:0:repo_len}" "$hash_sep" "$hash"
}

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

normalize_runner_version() {
  local raw="${1#v}"
  [[ "$raw" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  printf '%s' "$raw"
}

read_secret_from_tty() {
  local prompt="$1" value
  [[ -r /dev/tty && -w /dev/tty ]] || die "Interactive token input requires a TTY."
  IFS= read -r -s -p "$prompt" value < /dev/tty
  printf '\n' > /dev/tty
  printf '%s' "$value"
}

read_secret_from_fd() {
  local fd="$1" value
  [[ "$fd" =~ ^[0-9]+$ ]] || die "Invalid token FD."
  IFS= read -r value <&"$fd" || true
  [[ -n "$value" ]] || die "Token input cannot be empty."
  printf '%s' "$value"
}

web_priv_request() {
  local payload="$1" response
  [[ "$WEB_MODE" == "1" && "$PRIVILEGED_FD" =~ ^[0-9]+$ ]] || die "Invalid Web privileged context."
  printf '%s\n' "$payload" >&"$PRIVILEGED_FD" || die "Privileged dispatcher channel write failed."
  IFS= read -r response <&"$PRIVILEGED_FD" || die "Privileged dispatcher channel closed."
  WEB_PRIV_RESPONSE="$response"
  jq -e '.ok == true' <<<"$response" >/dev/null 2>&1
}

cli_create_authority() {
  local verb="$1" action="$2" version="$3"
  local authority="/usr/local/lib/github-runner-tools/web/cli_create_authority.py"
  [[ "$WEB_MODE" != "1" ]] || die "CLI root lifecycle authority invoked in Web context."
  [[ -f "$authority" && ! -L "$authority" ]] ||
    die "Trusted installed CLI lifecycle authority missing; install the approved tooling before Create."
  [[ "$(stat -c '%U:%G:%a' "$authority")" == "root:root:755" ]] ||
    die "CLI lifecycle authority ownership/mode invalid."
  sudo /usr/bin/python3 "$authority" "$verb" "$REPO" "$RUNNER_NAME" "$RUNNER_DIR" "$action" "$version" ||
    die "CLI lifecycle integrity checkpoint failed; keep registered resources for admin review."
}

web_create_stage() {
  local stage="$1" request
  [[ "$WEB_MODE" == "1" ]] || die "Create-state authority requires Web context."
  request="$(jq -nc --arg repository "$REPO" --arg runner_dir "$RUNNER_DIR" \
    --arg runner_name "$RUNNER_NAME" --arg stage "$stage" \
    '{op:"create_state",repository:$repository,runner_dir:$runner_dir,runner_name:$runner_name,stage:$stage}')"
  web_priv_request "$request" || die "Durable Create state transition failed."
}

web_context_check() {
  [[ "$PRIVILEGED_FD" =~ ^[0-9]+$ ]] || die "Invalid Web privileged context."
  python3 - "$PRIVILEGED_FD" <<'PY' || die "Web lifecycle privileged channel is not owned by the root dispatcher."
import os
import socket
import struct
import sys

fd = int(sys.argv[1])
sock = socket.socket(fileno=os.dup(fd))
try:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
finally:
    sock.close()
if uid != 0:
    raise SystemExit(1)
PY
  web_priv_request '{"op":"context_check"}' || die "Web lifecycle context is not authorized by dispatcher."
}

ensure_mutation_lock() {
  local group gid
  if [[ "${GRT_TEST_MODE:-0}" == "1" ]]; then
    MUTATION_LOCK_DIR="${GRT_TEST_LOCK_DIR:-${TMPDIR:-/tmp}/github-runner-tools-test-lock-$(id -u)}"
    MUTATION_LOCK_FILE="$MUTATION_LOCK_DIR/mutation.lock"
    mkdir -p -- "$MUTATION_LOCK_DIR"
    : > "$MUTATION_LOCK_FILE"
    chmod 0660 "$MUTATION_LOCK_FILE"
    return 0
  fi

  group="$(id -gn)"
  gid="$(id -g)"
  if [[ -L "$MUTATION_LOCK_DIR" ]]; then
    die "Mutation lock directory must not be a symlink."
  fi
  if [[ ! -e "$MUTATION_LOCK_DIR" ]]; then
    sudo install -d -o root -g root -m 0755 "$MUTATION_LOCK_DIR"
  fi
  [[ -d "$MUTATION_LOCK_DIR" && ! -L "$MUTATION_LOCK_DIR" ]] || die "Mutation lock directory has invalid type."
  [[ "$(stat -c '%u' "$MUTATION_LOCK_DIR")" == "0" && "$(stat -c '%a' "$MUTATION_LOCK_DIR")" == "755" ]] ||
    die "Mutation lock directory has invalid ownership/mode."

  if [[ -L "$MUTATION_LOCK_FILE" ]]; then
    die "Mutation lock file must not be a symlink."
  fi
  if [[ ! -e "$MUTATION_LOCK_FILE" ]]; then
    sudo touch "$MUTATION_LOCK_FILE"
    sudo chown root:"$group" "$MUTATION_LOCK_FILE"
    sudo chmod 0660 "$MUTATION_LOCK_FILE"
  fi
  [[ -f "$MUTATION_LOCK_FILE" && ! -L "$MUTATION_LOCK_FILE" ]] || die "Mutation lock file has invalid type."
  [[ "$(stat -c '%u' "$MUTATION_LOCK_FILE")" == "0" &&
     "$(stat -c '%g' "$MUTATION_LOCK_FILE")" == "$gid" &&
     "$(stat -c '%a' "$MUTATION_LOCK_FILE")" == "660" ]] ||
    die "Mutation lock file has invalid ownership/mode."
}

acquire_mutation_lock() {
  if [[ "$WEB_MODE" == "1" ]]; then
    [[ "$LOCK_ALREADY_HELD" == "1" ]] || die "Web lifecycle requires dispatcher-held mutation lock."
    web_context_check
    return 0
  fi
  ensure_mutation_lock
  exec {MUTATION_LOCK_FD}<>"$MUTATION_LOCK_FILE"
  flock -n "$MUTATION_LOCK_FD" || die "Another runner lifecycle operation is already in progress."
}

validate_local_archive_platform() {
  [[ -r "$LOCAL_ARCHIVE_LIB" ]] || die "Local archive platform is not installed. Run scripts/setup-local-archive.sh first."
  [[ -r "$LOCAL_ARCHIVE_CONFIG" ]] || die "Local archive config is missing: $LOCAL_ARCHIVE_CONFIG"
  [[ -x "$LOCAL_ARCHIVE_HOOK" ]] || die "Local archive completed hook is missing or not executable: $LOCAL_ARCHIVE_HOOK"
  # shellcheck source=/dev/null
  source "$LOCAL_ARCHIVE_LIB"
  grt_load_archive_config "$LOCAL_ARCHIVE_CONFIG" || die "Local archive config is invalid."
  [[ -d "$ARCHIVE_ROOT" && ! -L "$ARCHIVE_ROOT" && -x "$ARCHIVE_ROOT" && -r "$ARCHIVE_ROOT" && -w "$ARCHIVE_ROOT" ]] || die "Archive root is not accessible and writable by $RUNNER_USER: $ARCHIVE_ROOT"
  grt_is_world_writable "$ARCHIVE_ROOT" || die "Archive root must not be world-writable."
  if [[ "${GRT_TEST_MODE:-0}" != "1" || "${GITHUB_ACTIONS:-}" == "true" ]]; then
    [[ "$(stat -c '%u' "$LOCAL_ARCHIVE_CONFIG")" == "0" && ! -w "$LOCAL_ARCHIVE_CONFIG" ]] || die "Archive config must be root-owned and not writable by $RUNNER_USER."
    [[ "$(stat -c '%u' "$LOCAL_ARCHIVE_HOOK")" == "0" && ! -w "$LOCAL_ARCHIVE_HOOK" ]] || die "Shared archive hook must be root-owned and not writable by $RUNNER_USER."
  fi
  (( MIN_FREE_PERCENT >= 1 && MIN_FREE_PERCENT <= 99 )) || die "Archive disk guard threshold is invalid."
}

configure_runner_archive_hook() {
  local env_file="$1" current=""
  if current="$(grt_read_runner_env_value "$env_file" ACTIONS_RUNNER_HOOK_JOB_COMPLETED 2>/dev/null)"; then
    [[ "$current" == "$LOCAL_ARCHIVE_HOOK" ]] || die "Runner .env contains a conflicting completed hook: $current"
    return 0
  fi
  grt_set_runner_env_value "$env_file" ACTIONS_RUNNER_HOOK_JOB_COMPLETED "$LOCAL_ARCHIVE_HOOK" || die "Could not configure completed hook in $env_file"
}

main() {
if [[ ${EUID} -eq 0 ]]; then die "Do not run this script as root. Run it as the user that should own the runner."; fi
# Secure all official registration files from first creation, regardless of the
# shell inherited by the Web worker or interactive CLI.
umask 077

CLEAN_INCOMPLETE=0
CLI_BASE_DIR=""; CLI_RUNNER_NAME=""; CLI_LABELS=""; CLI_RUNNER_VERSION=""
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir) [[ $# -ge 2 ]] || die "--base-dir requires a value"; CLI_BASE_DIR="$2"; shift 2 ;;
    --runner-name) [[ $# -ge 2 ]] || die "--runner-name requires a value"; CLI_RUNNER_NAME="$2"; shift 2 ;;
    --labels) [[ $# -ge 2 ]] || die "--labels requires a value"; CLI_LABELS="$2"; shift 2 ;;
    --runner-version) [[ $# -ge 2 ]] || die "--runner-version requires a value"; CLI_RUNNER_VERSION="$2"; shift 2 ;;
    --clean-incomplete) CLEAN_INCOMPLETE=1; shift ;;
    --web-worker) WEB_MODE=1; shift ;;
    --token-fd) [[ $# -ge 2 ]] || die "--token-fd requires a value"; TOKEN_FD="$2"; shift 2 ;;
    --privileged-fd) [[ $# -ge 2 ]] || die "--privileged-fd requires a value"; PRIVILEGED_FD="$2"; shift 2 ;;
    --lock-already-held) LOCK_ALREADY_HELD=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    -*) die "Unknown option: $1" ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
set -- "${POSITIONAL[@]}"
[[ $# -eq 1 ]] || { usage; exit 1; }

if [[ "$WEB_MODE" == "1" ]]; then
  [[ "${GRT_WEB_CONTEXT:-0}" == "1" ]] || die "Internal Web mode requires dispatcher context."
  [[ "$TOKEN_FD" =~ ^[0-9]+$ && "$PRIVILEGED_FD" =~ ^[0-9]+$ && "$LOCK_ALREADY_HELD" == "1" ]] ||
    die "Incomplete internal Web lifecycle context."
elif [[ -n "$TOKEN_FD" || -n "$PRIVILEGED_FD" || "$LOCK_ALREADY_HELD" == "1" ]]; then
  die "Internal Web options are not available in normal CLI mode."
fi

REPO="$1"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "Repository must be in OWNER/REPO form."
OWNER="${REPO%%/*}"; REPO_NAME="${REPO##*/}"
SAFE_REPO="$(sanitize_component "$REPO_NAME")" || die "Could not derive a safe repository label from: $REPO"
LOCAL_ID="$(make_local_id "$OWNER" "$REPO_NAME")" || die "Could not derive a safe local identity from: $REPO"
(( ${#LOCAL_ID} <= MAX_LOCAL_ID_LENGTH )) || die "Internal error: local identity exceeds ${MAX_LOCAL_ID_LENGTH} characters"

for cmd in bash curl tar jq sha256sum uname id ps sed tr awk find mktemp cut xargs systemctl stat grep flock python3; do require_command "$cmd"; done
if [[ "$WEB_MODE" != "1" ]]; then require_command sudo; fi

REQUESTED_RUNNER_USER="${RUNNER_USER:-}"
RUNNER_USER="$(id -un)"
if [[ -n "$REQUESTED_RUNNER_USER" && "$REQUESTED_RUNNER_USER" != "$RUNNER_USER" ]]; then die "RUNNER_USER must match the user executing this script; cross-user provisioning is not supported in v1."; fi
USER_HOME="$(resolve_home "$RUNNER_USER")" || die "Could not resolve a valid home directory for user: $RUNNER_USER"
[[ -d "$USER_HOME" && -x "$USER_HOME" && -r "$USER_HOME" && -w "$USER_HOME" ]] || die "Runner user's home directory is not accessible and writable: $USER_HOME"

if [[ "$WEB_MODE" != "1" ]]; then
  sudo -v || die "sudo access is required for dependency and systemd service setup."
else
  web_context_check
fi
PID1="$(ps -p 1 -o comm= | xargs)"
[[ "$PID1" == "systemd" ]] || die "This version requires a systemd-based Linux host."
case "$(uname -m)" in
  x86_64|amd64) RUNNER_ARCH="x64"; RUNNER_ARCH_LABEL="X64" ;;
  aarch64|arm64) RUNNER_ARCH="arm64"; RUNNER_ARCH_LABEL="ARM64" ;;
  *) die "Unsupported CPU architecture: $(uname -m)" ;;
esac
if [[ "$WEB_MODE" != "1" ]]; then
  [[ -r /dev/tty && -w /dev/tty ]] || die "Interactive token input requires a TTY."
fi

validate_local_archive_platform
acquire_mutation_lock

RUNNER_BASE_DIR="${CLI_BASE_DIR:-${RUNNER_BASE_DIR:-$USER_HOME}}"
RUNNER_NAME="${CLI_RUNNER_NAME:-${RUNNER_NAME:-local-ci-$LOCAL_ID}}"
RUNNER_LABELS="${CLI_LABELS:-${RUNNER_LABELS:-local-ci,$SAFE_REPO}}"
REQUESTED_RUNNER_VERSION="${CLI_RUNNER_VERSION:-${RUNNER_VERSION:-}}"

if [[ "$RUNNER_BASE_DIR" != "$USER_HOME" ]]; then [[ -d "$RUNNER_BASE_DIR" ]] || die "Custom RUNNER_BASE_DIR must already exist: $RUNNER_BASE_DIR"; fi
[[ -d "$RUNNER_BASE_DIR" && -x "$RUNNER_BASE_DIR" && -r "$RUNNER_BASE_DIR" && -w "$RUNNER_BASE_DIR" ]] || die "RUNNER_BASE_DIR must be traversable, readable, and writable by $RUNNER_USER: $RUNNER_BASE_DIR"
RUNNER_BASE_DIR="$(canonicalize_existing_dir "$RUNNER_BASE_DIR")" || die "Could not canonicalize RUNNER_BASE_DIR: $RUNNER_BASE_DIR"
RUNNER_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"

if [[ -n "$REQUESTED_RUNNER_VERSION" ]]; then
  VERSION="$(normalize_runner_version "$REQUESTED_RUNNER_VERSION")" || die "Invalid runner version: $REQUESTED_RUNNER_VERSION (expected 2.328.0 or v2.328.0)"
  TAG="v$VERSION"; RUNNER_VERSION_DISPLAY="$TAG"
else
  VERSION=""; TAG=""; RUNNER_VERSION_DISPLAY="latest"
fi

cat <<CONFIG
Repository   : $REPO
Runner user  : $RUNNER_USER
Home         : $USER_HOME
Base dir     : $RUNNER_BASE_DIR
Local ID     : $LOCAL_ID
Runner dir   : $RUNNER_DIR
Runner name  : $RUNNER_NAME
Labels       : $RUNNER_LABELS
Architecture : $RUNNER_ARCH_LABEL ($RUNNER_ARCH asset)
Runner ver.  : $RUNNER_VERSION_DISPLAY
CONFIG

if [[ -e "$RUNNER_DIR/.runner" ]]; then die "A configured runner already exists at $RUNNER_DIR."; fi
if [[ -d "$RUNNER_DIR" && -n "$(find "$RUNNER_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  if [[ "$CLEAN_INCOMPLETE" -eq 1 ]]; then rm -rf -- "$RUNNER_DIR"; else die "An unconfigured non-empty runner directory already exists: $RUNNER_DIR (inspect it or retry with --clean-incomplete)"; fi
fi
if [[ ! -d "$RUNNER_DIR" ]]; then mkdir -- "$RUNNER_DIR"; CREATED_RUNNER_DIR=1; fi
cd "$RUNNER_DIR"

if [[ "$WEB_MODE" != "1" ]]; then
  TOKEN="$(read_secret_from_tty "Paste GitHub registration token: ")"
  [[ -n "$TOKEN" ]] || die "Registration token cannot be empty."
fi

RELEASE_JSON="$(mktemp)"
if [[ -n "$TAG" ]]; then API_URL="https://api.github.com/repos/actions/runner/releases/tags/$TAG"; echo "==> Fetching official GitHub Actions Runner release $TAG..."; else API_URL="https://api.github.com/repos/actions/runner/releases/latest"; echo "==> Fetching latest official GitHub Actions Runner release..."; fi
curl -fsSL --retry 3 --retry-delay 2 "$API_URL" -o "$RELEASE_JSON" || die "Could not fetch GitHub Actions Runner release metadata."

if [[ -z "$TAG" ]]; then
  TAG="$(jq -er '.tag_name' "$RELEASE_JSON")" || die "Release metadata did not contain tag_name."
  VERSION="${TAG#v}"
else
  ACTUAL_TAG="$(jq -er '.tag_name' "$RELEASE_JSON")" || die "Release metadata did not contain tag_name."
  [[ "$ACTUAL_TAG" == "$TAG" ]] || die "Requested runner release $TAG but GitHub returned $ACTUAL_TAG"
fi
ARCHIVE="actions-runner-linux-${RUNNER_ARCH}-${VERSION}.tar.gz"
DOWNLOAD_URL="$(jq -er --arg NAME "$ARCHIVE" '.assets[] | select(.name == $NAME) | .browser_download_url' "$RELEASE_JSON")" || die "Could not find $ARCHIVE in official release $TAG"
DIGEST="$(jq -r --arg NAME "$ARCHIVE" '.assets[] | select(.name == $NAME) | (.digest // empty)' "$RELEASE_JSON")"
ARCHIVE_PATH="$RUNNER_DIR/$ARCHIVE"

echo "==> Runner release: $TAG"
echo "==> Downloading $ARCHIVE..."
curl -fL --retry 3 --retry-delay 2 -o "$ARCHIVE_PATH" "$DOWNLOAD_URL" || die "Runner download failed."
if [[ "$DIGEST" == sha256:* ]]; then
  EXPECTED_SHA256="${DIGEST#sha256:}"; ACTUAL_SHA256="$(sha256sum "$ARCHIVE_PATH" | awk '{print $1}')"
  [[ "$EXPECTED_SHA256" == "$ACTUAL_SHA256" ]] || die "SHA-256 verification failed for $ARCHIVE"
  echo "==> SHA-256 verification: OK"
else
  echo "WARNING: GitHub release metadata did not provide a SHA-256 digest for this asset." >&2
fi

echo "==> Extracting runner..."
tar xzf "$ARCHIVE_PATH"; rm -f -- "$ARCHIVE_PATH"; ARCHIVE_PATH=""
echo "==> Checking/installing official runner dependencies..."
if [[ "$WEB_MODE" == "1" ]]; then
  if command -v ldd >/dev/null 2>&1 && ldd ./bin/Runner.Listener 2>/dev/null | grep -q 'not found'; then
    die "Runner dependencies are missing. Re-run Web platform setup before creating a runner."
  fi
else
  sudo ./bin/installdependencies.sh
fi

if [[ "$WEB_MODE" == "1" ]]; then
  web_create_stage PRE_REGISTRATION
else
  cli_create_authority stage PRE_REGISTRATION -
fi

CREATE_REQUEST_STARTED=1
echo "==> Registering runner for https://github.com/$REPO ..."
if [[ "$WEB_MODE" == "1" ]]; then
  PTY_ADAPTER="${GRT_PTY_ADAPTER:-/usr/local/lib/github-runner-tools/web/pty_token_adapter.py}"
  [[ -x "$PTY_ADAPTER" ]] || die "Web PTY token adapter is unavailable."
  python3 "$PTY_ADAPTER" --token-fd "$TOKEN_FD" --mode create -- \
    ./config.sh \
      --url "https://github.com/$REPO" \
      --name "$RUNNER_NAME" \
      --labels "$RUNNER_LABELS" \
      --work "_work" ||
    { web_create_stage REGISTRATION_OUTCOME_UNKNOWN || true; die "Runner registration outcome is uncertain; preserve local registration state and reconcile with GitHub before retry."; }
else
  ./config.sh \
    --url "https://github.com/$REPO" \
    --token "$TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work "_work" \
    --unattended || {
      cli_create_authority stage REGISTRATION_OUTCOME_UNKNOWN - || true
      die "Runner registration outcome is uncertain; preserve local directory and investigate before retry."
    }
  unset TOKEN
fi
REGISTRATION_COMPLETE=1

if [[ "$WEB_MODE" == "1" ]]; then
  web_create_stage REGISTERED_PERMISSION_INCOMPLETE
  NORMALIZE_REQ="$(jq -nc --arg repository "$REPO" --arg runner_dir "$RUNNER_DIR" \
    --arg runner_name "$RUNNER_NAME" \
    '{op:"registration_permission_normalize",repository:$repository,runner_dir:$runner_dir,runner_name:$runner_name}')"
  web_priv_request "$NORMALIZE_REQ" || die "New registration permissions could not be normalized safely."
  CANONICAL_SERVICE="actions.runner.${OWNER}-${REPO_NAME}.${RUNNER_NAME}.service"
  python3 "${BASH_SOURCE[0]%/*}/web-service-record.py" registration_check \
    "$RUNNER_BASE_DIR" "$RUNNER_DIR" "$REPO" "$RUNNER_NAME" "$CANONICAL_SERVICE" ||
    die "New Runner registration metadata is not safely restricted to owner-only permissions."
  ATTEST_REQ="$(jq -nc --arg repository "$REPO" --arg runner_dir "$RUNNER_DIR" \
    --arg runner_name "$RUNNER_NAME" --arg version "$VERSION" \
    '{op:"registration_attestation_create",repository:$repository,runner_dir:$runner_dir,runner_name:$runner_name,version:$version}')"
  web_priv_request "$ATTEST_REQ" || die "Root-controlled registration attestation failed."
  web_create_stage REGISTERED_UNIT_INCOMPLETE
else
  cli_create_authority stage REGISTERED_PERMISSION_INCOMPLETE -
  cli_create_authority normalize - -
  CANONICAL_SERVICE="actions.runner.${OWNER}-${REPO_NAME}.${RUNNER_NAME}.service"
  python3 "${BASH_SOURCE[0]%/*}/web-service-record.py" registration_check \
    "$RUNNER_BASE_DIR" "$RUNNER_DIR" "$REPO" "$RUNNER_NAME" "$CANONICAL_SERVICE" ||
    die "CLI registration metadata is not owner-only; remote registration retained."
  cli_create_authority attest - "$VERSION"
  cli_create_authority stage REGISTERED_UNIT_INCOMPLETE -
fi

echo "==> Configuring local artifact completed hook..."
configure_runner_archive_hook "$RUNNER_DIR/.env"

if [[ "$WEB_MODE" == "1" ]]; then
  INSTALL_REQ="$(jq -nc --arg repository "$REPO" --arg runner_dir "$RUNNER_DIR" --arg runner_name "$RUNNER_NAME" \
    '{op:"service_install",repository:$repository,runner_dir:$runner_dir,runner_name:$runner_name}')"
  web_priv_request "$INSTALL_REQ" || die "Privileged canonical service installation failed."
  SERVICE_NAME="$(jq -er '.service' <<<"$WEB_PRIV_RESPONSE")" || die "Privileged service installer returned invalid identity."
  printf '%s\n' "$SERVICE_NAME" > "$RUNNER_DIR/.service"

  web_create_stage REGISTERED_START_INCOMPLETE

  START_REQ="$(jq -nc --arg repository "$REPO" --arg runner_dir "$RUNNER_DIR" --arg runner_name "$RUNNER_NAME" --arg service "$SERVICE_NAME" \
    '{op:"service_start",repository:$repository,runner_dir:$runner_dir,runner_name:$runner_name,service:$service}')"
  web_priv_request "$START_REQ" || die "Privileged runner service start failed."
  web_create_stage REGISTERED_HEALTH_UNKNOWN
  CHECK_REQ="$(jq -nc --arg repository "$REPO" --arg runner_dir "$RUNNER_DIR" --arg runner_name "$RUNNER_NAME" --arg service "$SERVICE_NAME" \
    '{op:"service_state",repository:$repository,runner_dir:$runner_dir,runner_name:$runner_name,service:$service}')"
  web_priv_request "$CHECK_REQ" || die "Post-start service health verification failed."
  [[ "$(jq -r '.state // empty' <<<"$WEB_PRIV_RESPONSE")" == "active" ]] ||
    die "Registered Runner service has not reached a confirmed active state."
  web_create_stage CREATE_COMPLETE
else
  echo "==> Installing systemd service for user $RUNNER_USER ..."
  sudo ./svc.sh install "$RUNNER_USER" ||
    die "CLI Unit installation incomplete; remote registration preserved."
  # The official v2.328.0 installer may create a group-writable 0664 Unit.
  # No unsafe post-hoc chmod, no service start and no false Create success.
  cli_create_authority unit - -
  cli_create_authority stage REGISTERED_START_INCOMPLETE -
  echo "==> Starting runner service..."
  sudo ./svc.sh start ||
    die "CLI service start incomplete; remote registration preserved."
  cli_create_authority stage REGISTERED_HEALTH_UNKNOWN -
  echo
  echo "==> Runner status"
  sudo ./svc.sh status ||
    die "CLI service health unknown; remote registration preserved."
  cli_create_authority stage CREATE_COMPLETE -
fi

echo
cat <<DONE
============================================================
Runner registration completed.

Repository   : https://github.com/$REPO
Runner name  : $RUNNER_NAME
Directory    : $RUNNER_DIR
Architecture : $RUNNER_ARCH_LABEL
Runner ver.  : $TAG
Labels       : self-hosted, Linux, $RUNNER_ARCH_LABEL, $RUNNER_LABELS
Archive hook : $LOCAL_ARCHIVE_HOOK
Archive root : $ARCHIVE_ROOT

GitHub page:
  https://github.com/$REPO/settings/actions/runners

Recommended workflow selector:
  runs-on: [self-hosted, Linux, $RUNNER_ARCH_LABEL, $SAFE_REPO]
============================================================
DONE

}

if [[ "${RUNNER_TOOLS_LIB_ONLY:-0}" != "1" ]]; then
  trap cleanup EXIT
  main "$@"
fi
