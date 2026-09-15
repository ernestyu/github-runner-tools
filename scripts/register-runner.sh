#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/register-runner.sh OWNER/REPO

Optional environment variables:
  RUNNER_BASE_DIR   Parent directory that will contain actions-runner-<repo>
  RUNNER_NAME       Override runner name
  RUNNER_LABELS     Override custom labels, comma-separated

Example:
  bash scripts/register-runner.sh ernestyu/CycleEdge
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

if [[ ${EUID} -eq 0 ]]; then
  die "Do not run this script as root. Run it as the normal runner user, for example: actions"
fi

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

REPO="$1"
if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  die "Repository must be in OWNER/REPO form, for example: ernestyu/CycleEdge"
fi

for cmd in curl jq tar sha256sum sudo dirname tr sed mktemp; do
  require_command "$cmd"
done

OWNER="${REPO%%/*}"
REPO_NAME="${REPO##*/}"
SAFE_NAME="$(printf '%s' "$REPO_NAME" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^-+//; s/-+$//')"
[[ -n "$SAFE_NAME" ]] || die "Could not derive a safe runner name from repository: $REPO"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOOL_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DEFAULT_BASE_DIR="$(cd -- "$TOOL_ROOT/.." && pwd)"
RUNNER_BASE_DIR="${RUNNER_BASE_DIR:-$DEFAULT_BASE_DIR}"
RUNNER_DIR="$RUNNER_BASE_DIR/actions-runner-$SAFE_NAME"
RUNNER_NAME="${RUNNER_NAME:-unraid-ci-$SAFE_NAME}"
RUNNER_LABELS="${RUNNER_LABELS:-unraid-ci,$SAFE_NAME}"

cat <<EOF
Repository : $REPO
Tool root  : $TOOL_ROOT
Runner base: $RUNNER_BASE_DIR
Runner dir : $RUNNER_DIR
Runner name: $RUNNER_NAME
Labels     : $RUNNER_LABELS
EOF

if [[ -e "$RUNNER_DIR/.runner" ]]; then
  die "A configured runner already exists at $RUNNER_DIR. Remove it first or choose another RUNNER_BASE_DIR/RUNNER_NAME."
fi

if [[ -d "$RUNNER_DIR" ]] && [[ -n "$(find "$RUNNER_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  die "Runner directory already exists and is not empty: $RUNNER_DIR"
fi

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

read -rsp "Paste GitHub registration token: " TOKEN
echo
[[ -n "$TOKEN" ]] || die "Registration token cannot be empty."

API_URL="https://api.github.com/repos/actions/runner/releases/latest"
echo "==> Fetching latest official GitHub Actions Runner release..."
RELEASE_JSON="$(mktemp)"
trap 'rm -f "$RELEASE_JSON"' EXIT
curl -fsSL --retry 3 --retry-delay 2 "$API_URL" -o "$RELEASE_JSON"

TAG="$(jq -er '.tag_name' "$RELEASE_JSON")"
VERSION="${TAG#v}"
ARCHIVE="actions-runner-linux-x64-${VERSION}.tar.gz"
DOWNLOAD_URL="$(jq -er --arg NAME "$ARCHIVE" '.assets[] | select(.name == $NAME) | .browser_download_url' "$RELEASE_JSON")"
DIGEST="$(jq -r --arg NAME "$ARCHIVE" '.assets[] | select(.name == $NAME) | (.digest // empty)' "$RELEASE_JSON")"

[[ -n "$DOWNLOAD_URL" ]] || die "Could not find Linux x64 runner asset for release $TAG"

echo "==> Latest runner: $TAG"
echo "==> Downloading $ARCHIVE..."
curl -fL --retry 3 --retry-delay 2 -o "$ARCHIVE" "$DOWNLOAD_URL"

if [[ "$DIGEST" == sha256:* ]]; then
  EXPECTED_SHA256="${DIGEST#sha256:}"
  ACTUAL_SHA256="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
  if [[ "$EXPECTED_SHA256" != "$ACTUAL_SHA256" ]]; then
    rm -f "$ARCHIVE"
    die "SHA-256 verification failed for $ARCHIVE"
  fi
  echo "==> SHA-256 verification: OK"
else
  echo "WARNING: GitHub release metadata did not provide a SHA-256 digest for this asset." >&2
  echo "WARNING: Continuing because the file was downloaded directly from the official actions/runner release over HTTPS." >&2
fi

echo "==> Extracting runner..."
tar xzf "$ARCHIVE"
rm -f "$ARCHIVE"

echo "==> Installing official runner dependencies..."
sudo ./bin/installdependencies.sh

echo "==> Registering runner for https://github.com/$REPO ..."
./config.sh \
  --url "https://github.com/$REPO" \
  --token "$TOKEN" \
  --name "$RUNNER_NAME" \
  --labels "$RUNNER_LABELS" \
  --work "_work" \
  --unattended \
  --replace

unset TOKEN

echo "==> Installing systemd service for user $USER ..."
sudo ./svc.sh install "$USER"

echo "==> Starting runner service..."
sudo ./svc.sh start

echo
echo "==> Runner status"
sudo ./svc.sh status

echo
cat <<EOF
============================================================
Runner registration completed.

Repository : https://github.com/$REPO
Runner name: $RUNNER_NAME
Directory  : $RUNNER_DIR
Labels     : self-hosted, Linux, X64, $RUNNER_LABELS

GitHub page:
  https://github.com/$REPO/settings/actions/runners

Recommended workflow selector:
  runs-on: [self-hosted, Linux, X64, $SAFE_NAME]
============================================================
EOF
