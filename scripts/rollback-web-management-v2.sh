#!/usr/bin/env bash
set -Eeuo pipefail
# Explicit rollback of the Web process/config only. Never touch runner trees
# or revert lifecycle records; only a reviewed state-compatible rollback.
STATE_DIR="/var/lib/github-runner-tools"
INSTALL_ROOT="/usr/local/lib/github-runner-tools/web"
CONFIG="/etc/github-runner-tools/web.conf"
AUTH="/etc/github-runner-tools/web-auth.conf"
WEB_UNIT="/etc/systemd/system/github-runner-tools-web.service"
EXEC_UNIT="/etc/systemd/system/github-runner-tools-executor.service"
V2_MARKER="# managed-by=github-runner-tools-web-v2"
[[ "${1:-}" == "--dry-run" || "${1:-}" == "--apply" ]] ||
  { echo "Usage: rollback-web-management-v2.sh [--dry-run|--apply] --backup-dir ABSOLUTE" >&2; exit 2; }
[[ "${2:-}" == "--backup-dir" && $# -eq 3 ]] || exit 2
MODE="$1"
BACKUP="$3"
[[ "$EUID" -ne 0 && "$(id -un)" == actions ]] ||
  { echo "Rollback must be started by the runner owner" >&2; exit 1; }
[[ "$BACKUP" =~ ^/var/lib/github-runner-tools/web-v2-rollback\.[A-Za-z0-9]{8}$ ]] ||
  { echo "Unsafe rollback backup location" >&2; exit 1; }
for cmd in sudo systemctl stat grep cp install; do
  command -v "$cmd" >/dev/null || exit 1
done
if [[ "$MODE" == "--dry-run" ]]; then
  echo "Would restore V1 Web code/config from root-only backup (not lifecycle state)."
  echo "No runner services or state records would be modified."
  exit 0
fi
sudo -v
sudo test -d "$BACKUP" && ! sudo test -L "$BACKUP" ||
  { echo "Rollback backup missing or unsafe" >&2; exit 1; }
sudo grep -Fqx "$V2_MARKER" "$WEB_UNIT" ||
  { echo "Current Web Unit is not recognized V2-managed code" >&2; exit 1; }
sudo grep -Fqx "$V2_MARKER" "$EXEC_UNIT" ||
  { echo "Current Executor Unit is not V2-managed" >&2; exit 1; }
for target in web.conf web-auth.conf github-runner-tools-web.service; do
  sudo test -f "$BACKUP/$target" && ! sudo test -L "$BACKUP/$target" ||
    { echo "Incomplete V1 backup" >&2; exit 1; }
done
sudo test -d "$BACKUP/installed-web" &&
  ! sudo test -L "$BACKUP/installed-web" ||
  { echo "Missing installed V1 code backup" >&2; exit 1; }

# This tool intentionally cannot restore the lifecycle-snapshot: preserving
# V2's current durable state is mandatory. Stop if compatibility is unknown.
echo "Rollback preserves current lifecycle state; verify schema compatibility first."
sudo systemctl stop github-runner-tools-web.service
sudo systemctl stop github-runner-tools-executor.service
sudo cp -a "$BACKUP/installed-web/." "$INSTALL_ROOT/"
sudo install -o root -g grt-web -m 0640 "$BACKUP/web.conf" "$CONFIG"
sudo install -o root -g grt-web -m 0640 "$BACKUP/web-auth.conf" "$AUTH"
sudo install -o root -g root -m 0644 "$BACKUP/github-runner-tools-web.service" "$WEB_UNIT"
sudo systemctl disable github-runner-tools-executor.service
sudo systemctl daemon-reload
sudo systemctl enable --now github-runner-tools-dispatch.service
sudo systemctl restart github-runner-tools-web.service
echo "V1 Web restored. No lifecycle state, GitHub runner or runner service was changed."
