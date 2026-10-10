#!/usr/bin/env bash
set -Eeuo pipefail
# Explicit V1 -> V2 Web service switch. Does not mutate runner services.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
INSTALL_ROOT="/usr/local/lib/github-runner-tools/web"
STATE_DIR="/var/lib/github-runner-tools"
CONFIG="/etc/github-runner-tools/web.conf"
AUTH="/etc/github-runner-tools/web-auth.conf"
WEB_UNIT="/etc/systemd/system/github-runner-tools-web.service"
EXEC_UNIT="/etc/systemd/system/github-runner-tools-executor.service"
RUNNER_HOME="/home/actions"
MARKER_V1="# managed-by=github-runner-tools-web-v1"
MARKER_V2="# managed-by=github-runner-tools-web-v2"
MODE="dry-run"
case "${1:---dry-run}" in
  --dry-run) [[ $# -le 1 ]] || exit 2 ;;
  --apply) [[ $# -eq 1 ]] || exit 2; MODE="apply" ;;
  *) echo "Usage: setup-web-management-v2.sh [--dry-run|--apply]" >&2; exit 2 ;;
esac
[[ "$EUID" -ne 0 ]] || { echo "Run as authorized runner owner, not root" >&2; exit 1; }
[[ "$(id -un)" == actions ]] || { echo "This upgrade requires the configured actions runner user" >&2; exit 1; }
for cmd in sudo systemctl install stat mktemp getent grep; do
  command -v "$cmd" >/dev/null || { echo "Missing dependency: $cmd" >&2; exit 1; }
done
echo "Web V2 upgrade: mode=$MODE, existing runner services untouched"
echo "Executor: /run/github-runner-tools/executor.sock, root:grt-web 0660"
if [[ "$MODE" == "dry-run" ]]; then
  echo "DRY-RUN ONLY. Explicit --apply and separate Debian authorization required."
  exit 0
fi
sudo -v
for file in "$CONFIG" "$AUTH" "$WEB_UNIT"; do
  sudo test -f "$file" && ! sudo test -L "$file" ||
    { echo "Missing or symlinked V1 Web file: $file" >&2; exit 1; }
done
sudo grep -Fqx "$MARKER_V1" "$CONFIG" ||
  { echo "Existing Web config is not V1-managed" >&2; exit 1; }
sudo grep -Fqx "$MARKER_V1" "$WEB_UNIT" ||
  { echo "Existing Web Unit is not V1-managed" >&2; exit 1; }
sudo test -f "$INSTALL_ROOT/.managed-by-github-runner-tools-web-v1" ||
  { echo "V1 installation marker missing" >&2; exit 1; }
if sudo test -e "$EXEC_UNIT" || sudo test -L "$EXEC_UNIT"; then
  echo "Existing Executor Unit must be independently reconciled" >&2
  exit 1
fi
[[ "$(sudo stat -c '%U:%G:%a:%F' "$STATE_DIR")" == "root:root:700:directory" ]] ||
  { echo "Root-owned lifecycle state must remain intact" >&2; exit 1; }
[[ "$(sudo stat -c '%U:%G:%a:%F' "$INSTALL_ROOT")" == "root:root:755:directory" ]] ||
  { echo "Untrusted installed executable path" >&2; exit 1; }

# Preserve V1 Web settings, credentials and Unit for reviewed rollback.
BACKUP="$(sudo mktemp -d "$STATE_DIR/web-v2-rollback.XXXXXXXX")"
sudo chmod 0700 "$BACKUP"
sudo cp -a "$CONFIG" "$AUTH" "$WEB_UNIT" "$BACKUP/"
sudo cp -a "$INSTALL_ROOT" "$BACKUP/installed-web"
echo "Root-only backup recorded under $STATE_DIR (path redacted)"

# Install V2 source + existing authority and scripts into root-owned paths.
for file in v2_executor_contract.py v2_executor_transport.py v2_token_fd.py \
            v2_status.py v2_executor.py v2_executor_client.py v2_cli_launcher.py \
            v2_cli_spawn.py v2_mutation.py v2_privileged.py v2_binding.py \
            v2_event_loop.py v2_remove_guard.py v2_inventory.py v2_unit_installer.py \
            app.py dispatcher.py grt_web_common.py runner_lifecycle_authority.py \
            cli_create_authority.py pty_token_adapter.py; do
  [[ -f "$ROOT/web/$file" && ! -L "$ROOT/web/$file" ]] ||
    { echo "Missing fixed V2 module $file" >&2; exit 1; }
  sudo install -o root -g root -m 0755 "$ROOT/web/$file" "$INSTALL_ROOT/$file"
done
for file in register-runner.sh remove-runner.sh status-runners.sh \
            web-service-record.py web-runner-cleanup.py; do
  [[ -f "$ROOT/scripts/$file" && ! -L "$ROOT/scripts/$file" ]] ||
    { echo "Missing fixed CLI artifact $file" >&2; exit 1; }
  sudo install -o root -g root -m 0755 "$ROOT/scripts/$file" "$INSTALL_ROOT/cli/$file"
done

# Preserve all existing configuration fields; only opt into V2 routing.
TMP_CONFIG="$(mktemp)"
sudo cat "$CONFIG" > "$TMP_CONFIG"
if grep -q '^V2_EXECUTOR_MODE=' "$TMP_CONFIG"; then
  rm -f "$TMP_CONFIG"; echo "V2 config requires manual review" >&2; exit 1
fi
printf 'V2_EXECUTOR_MODE=1\n' >> "$TMP_CONFIG"
sudo install -o root -g grt-web -m 0640 "$TMP_CONFIG" "$CONFIG"
rm -f "$TMP_CONFIG"

TMP_WEB="$(mktemp)"
cat > "$TMP_WEB" <<EOF
$MARKER_V2
[Unit]
Description=github-runner-tools Web V2
After=network-online.target tailscaled.service github-runner-tools-executor.service
Wants=network-online.target
Requires=github-runner-tools-executor.service

[Service]
Type=simple
User=grt-web
Group=grt-web
ExecStart=/usr/bin/python3 $INSTALL_ROOT/app.py --config $CONFIG --auth-config $AUTH
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
EOF
TMP_EXEC="$(mktemp)"
cat > "$TMP_EXEC" <<EOF
$MARKER_V2
[Unit]
Description=github-runner-tools bounded Web V2 Executor
After=network-online.target systemd-tmpfiles-setup.service
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
RuntimeDirectory=github-runner-tools
RuntimeDirectoryMode=0755
ExecStart=/usr/bin/python3 $INSTALL_ROOT/v2_executor.py
Restart=on-failure
RestartSec=2
AmbientCapabilities=CAP_SETUID CAP_SETGID
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=$RUNNER_HOME /etc/systemd/system /run/github-runner-tools /run/lock/github-runner-tools $STATE_DIR

[Install]
WantedBy=multi-user.target
EOF
sudo install -o root -g root -m 0644 "$TMP_WEB" "$WEB_UNIT"
sudo install -o root -g root -m 0644 "$TMP_EXEC" "$EXEC_UNIT"
rm -f "$TMP_WEB" "$TMP_EXEC"
sudo systemctl daemon-reload
sudo systemctl enable --now github-runner-tools-executor.service
sudo systemctl restart github-runner-tools-web.service
sudo systemctl disable --now github-runner-tools-dispatch.service
echo "V2 Web switch attempted; verify status and authenticate before acceptance."
echo "If verification fails, stop operations and restore the reviewed V1 backup; never rewrite lifecycle records."
