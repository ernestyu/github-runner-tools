#!/usr/bin/env bash
set -Eeuo pipefail

MAX_LOCAL_ID_LENGTH=64
HASH_LENGTH=8
CREATED_RUNNER_DIR=0
REGISTRATION_COMPLETE=0
TOKEN=""
RELEASE_JSON=""
ARCHIVE_PATH=""

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
  if [[ "$CREATED_RUNNER_DIR" -eq 1 && "$REGISTRATION_COMPLETE" -eq 0 && -n "${RUNNER_DIR:-}" ]]; then
    if [[ -d "$RUNNER_DIR" && ! -e "$RUNNER_DIR/.runner" ]]; then rm -rf -- "$RUNNER_DIR" || true; fi
  fi
}
trap cleanup EXIT

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

main() {
if [[ ${EUID} -eq 0 ]]; then die "Do not run this script as root. Run it as the user that should own the runner."; fi

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
SAFE_REPO="$(sanitize_component "$REPO_NAME")" || die "Could not derive a safe repository label from: $REPO"
LOCAL_ID="$(make_local_id "$OWNER" "$REPO_NAME")" || die "Could not derive a safe local identity from: $REPO"
(( ${#LOCAL_ID} <= MAX_LOCAL_ID_LENGTH )) || die "Internal error: local identity exceeds ${MAX_LOCAL_ID_LENGTH} characters"

for cmd in bash curl tar jq sha256sum sudo uname id ps sed tr awk find mktemp cut xargs systemctl; do require_command "$cmd"; done

REQUESTED_RUNNER_USER="${RUNNER_USER:-}"
RUNNER_USER="$(id -un)"
if [[ -n "$REQUESTED_RUNNER_USER" && "$REQUESTED_RUNNER_USER" != "$RUNNER_USER" ]]; then die "RUNNER_USER must match the user executing this script; cross-user provisioning is not supported in v1."; fi
USER_HOME="$(resolve_home "$RUNNER_USER")" || die "Could not resolve a valid home directory for user: $RUNNER_USER"
[[ -d "$USER_HOME" && -x "$USER_HOME" && -r "$USER_HOME" && -w "$USER_HOME" ]] || die "Runner user's home directory is not accessible and writable: $USER_HOME"

sudo -v || die "sudo access is required for dependency and systemd service setup."
PID1="$(ps -p 1 -o comm= | xargs)"
[[ "$PID1" == "systemd" ]] || die "This version requires a systemd-based Linux host."
case "$(uname -m)" in
  x86_64|amd64) RUNNER_ARCH="x64"; RUNNER_ARCH_LABEL="X64" ;;
  aarch64|arm64) RUNNER_ARCH="arm64"; RUNNER_ARCH_LABEL="ARM64" ;;
  *) die "Unsupported CPU architecture: $(uname -m)" ;;
esac
[[ -r /dev/tty && -w /dev/tty ]] || die "Interactive token input requires a TTY."

RUNNER_BASE_DIR="${CLI_BASE_DIR:-${RUNNER_BASE_DIR:-$USER_HOME}}"
RUNNER_NAME="${CLI_RUNNER_NAME:-${RUNNER_NAME:-local-ci-$LOCAL_ID}}"
RUNNER_LABELS="${CLI_LABELS:-${RUNNER_LABELS:-local-ci,$SAFE_REPO}}"
REQUESTED_RUNNER_VERSION="${CLI_RUNNER_VERSION:-${RUNNER_VERSION:-}}"

if [[ "$RUNNER_BASE_DIR" != "$USER_HOME" ]]; then [[ -d "$RUNNER_BASE_DIR" ]] || die "Custom RUNNER_BASE_DIR must already exist: $RUNNER_BASE_DIR"; fi
[[ -d "$RUNNER_BASE_DIR" && -x "$RUNNER_BASE_DIR" && -r "$RUNNER_BASE_DIR" && -w "$RUNNER_BASE_DIR" ]] || die "RUNNER_BASE_DIR must be traversable, readable, and writable by $RUNNER_USER: $RUNNER_BASE_DIR"
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

TOKEN="$(read_secret_from_tty "Paste GitHub registration token: ")"
[[ -n "$TOKEN" ]] || die "Registration token cannot be empty."

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
echo "==> Installing official runner dependencies..."
sudo ./bin/installdependencies.sh

echo "==> Registering runner for https://github.com/$REPO ..."
./config.sh \
  --url "https://github.com/$REPO" \
  --token "$TOKEN" \
  --name "$RUNNER_NAME" \
  --labels "$RUNNER_LABELS" \
  --work "_work" \
  --unattended
REGISTRATION_COMPLETE=1
unset TOKEN

echo "==> Installing systemd service for user $RUNNER_USER ..."
sudo ./svc.sh install "$RUNNER_USER"
echo "==> Starting runner service..."
sudo ./svc.sh start

echo
echo "==> Runner status"
sudo ./svc.sh status

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

GitHub page:
  https://github.com/$REPO/settings/actions/runners

Recommended workflow selector:
  runs-on: [self-hosted, Linux, $RUNNER_ARCH_LABEL, $SAFE_REPO]
============================================================
DONE

}

if [[ "${RUNNER_TOOLS_LIB_ONLY:-0}" != "1" ]]; then
  main "$@"
fi
