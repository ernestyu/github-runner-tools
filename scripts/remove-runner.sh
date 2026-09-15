#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/remove-runner.sh OWNER/REPO

The script expects the default runner directory naming rule:
  actions-runner-<lowercase-repo-name>

Optional environment variable:
  RUNNER_BASE_DIR   Parent directory containing runner directories
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

if [[ ${EUID} -eq 0 ]]; then
  die "Do not run this script as root. Run it as the normal runner user."
fi

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

REPO="$1"
if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  die "Repository must be in OWNER/REPO form."
fi

REPO_NAME="${REPO##*/}"
SAFE_NAME="$(printf '%s' "$REPO_NAME" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^-+//; s/-+$//')"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOOL_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DEFAULT_BASE_DIR="$(cd -- "$TOOL_ROOT/.." && pwd)"
RUNNER_BASE_DIR="${RUNNER_BASE_DIR:-$DEFAULT_BASE_DIR}"
RUNNER_DIR="$RUNNER_BASE_DIR/actions-runner-$SAFE_NAME"

[[ -d "$RUNNER_DIR" ]] || die "Runner directory not found: $RUNNER_DIR"
[[ -x "$RUNNER_DIR/config.sh" ]] || die "config.sh not found in: $RUNNER_DIR"

cat <<EOF
Repository : $REPO
Runner dir : $RUNNER_DIR

Before continuing, open:
  https://github.com/$REPO/settings/actions/runners

Select the runner, choose Remove, and copy the temporary removal token.
EOF

read -rsp "Paste GitHub removal token: " TOKEN
echo
[[ -n "$TOKEN" ]] || die "Removal token cannot be empty."

read -rp "Type REMOVE to stop, unregister, and delete this runner: " CONFIRM
[[ "$CONFIRM" == "REMOVE" ]] || die "Cancelled."

cd "$RUNNER_DIR"

echo "==> Stopping service..."
sudo ./svc.sh stop || true

echo "==> Uninstalling service..."
sudo ./svc.sh uninstall || true

echo "==> Removing runner registration from GitHub..."
./config.sh remove --token "$TOKEN"
unset TOKEN

cd "$RUNNER_BASE_DIR"

case "$RUNNER_DIR" in
  "$RUNNER_BASE_DIR"/actions-runner-*) ;;
  *) die "Safety check failed; refusing to delete unexpected path: $RUNNER_DIR" ;;
esac

echo "==> Deleting local runner directory..."
rm -rf -- "$RUNNER_DIR"

echo
cat <<EOF
Runner removed successfully.
Repository: https://github.com/$REPO
Deleted:    $RUNNER_DIR
EOF
