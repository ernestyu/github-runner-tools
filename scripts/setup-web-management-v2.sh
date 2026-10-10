#!/usr/bin/env bash
set -Eeuo pipefail
# Explicit V1 -> V2 Web service switch. Does not mutate runner services.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
INSTALL_ROOT="/usr/local/lib/github-runner-tools/web"
CLI_ROOT="/usr/local/lib/github-runner-tools/cli"
STATE_DIR="/var/lib/github-runner-tools"
CONFIG="/etc/github-runner-tools/web.conf"
AUTH="/etc/github-runner-tools/web-auth.conf"
WEB_UNIT="/etc/systemd/system/github-runner-tools-web.service"
EXEC_UNIT="/etc/systemd/system/github-runner-tools-executor.service"
RUNNER_HOME="/home/actions"
MARKER_V1="# managed-by=github-runner-tools-web-v1"
MARKER_V2="# managed-by=github-runner-tools-web-v2"
MODE="dry-run"
EXPECTED_SHA=""
case "${1:---dry-run}" in
  --dry-run) [[ $# -le 1 ]] || exit 2 ;;
  --apply)
    [[ $# -eq 3 && "${2:-}" == "--expected-sha" ]] || {
      echo "Usage: setup-web-management-v2.sh --apply --expected-sha FULL40HEX" >&2; exit 2;
    }
    MODE="apply"; EXPECTED_SHA="$3" ;;
  *) echo "Usage: setup-web-management-v2.sh [--dry-run|--apply --expected-sha FULL40HEX]" >&2; exit 2 ;;
esac
[[ "$EUID" -ne 0 ]] || { echo "Run as authorized runner owner, not root" >&2; exit 1; }
for cmd in sudo systemctl install stat mktemp getent grep; do
  command -v "$cmd" >/dev/null || { echo "Missing dependency: $cmd" >&2; exit 1; }
done
echo "Web V2 upgrade: mode=$MODE, existing runner services untouched"
echo "Executor: /run/github-runner-tools/executor.sock, root:grt-web 0660"
if [[ "$MODE" == "dry-run" ]]; then
  echo "DRY-RUN ONLY. Explicit --apply and separate Debian authorization required."
  exit 0
fi
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] ||
  { echo "A full immutable implementation SHA is required" >&2; exit 1; }
command -v git >/dev/null ||
  { echo "Git is required to verify the implementation SHA" >&2; exit 1; }
[[ "$(git -C "$ROOT" rev-parse HEAD 2>/dev/null)" == "$EXPECTED_SHA" ]] ||
  { echo "Checked-out implementation does not match approved SHA" >&2; exit 1; }
[[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]] ||
  { echo "Working tree contains local changes; refusing deploy" >&2; exit 1; }
[[ "$(id -un)" == actions ]] ||
  { echo "This upgrade requires the configured actions runner user" >&2; exit 1; }
systemctl is-active --quiet tailscaled ||
  { echo "Tailscale is not active; do not expose Web backend" >&2; exit 1; }
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
sudo install -d -o root -g root -m 0700 "$BACKUP/lifecycle-snapshot"
# Snapshot only known root-owned authority folders. Never recursively copy
# STATE_DIR into its own backup, and never rewrite existing V1 records.
for state_subdir in active-instances runner-attestations unit-attestations \
                    create-state remove-state; do
  if sudo test -L "$STATE_DIR/$state_subdir"; then
    echo "Unsafe lifecycle state symlink: $state_subdir" >&2
    exit 1
  fi
  if sudo test -d "$STATE_DIR/$state_subdir"; then
    sudo cp -a "$STATE_DIR/$state_subdir" "$BACKUP/lifecycle-snapshot/"
  fi
done
echo "Root-only config, code and lifecycle snapshots recorded (path redacted)"

# Install V2 source + existing authority and scripts into root-owned paths.
for file in v2_executor_contract.py v2_executor_transport.py v2_token_fd.py \
            v2_status.py v2_executor.py v2_executor_client.py v2_cli_launcher.py \
            v2_cli_spawn.py v2_mutation.py v2_privileged.py v2_binding.py \
            v2_event_loop.py v2_remove_guard.py v2_inventory.py v2_unit_installer.py \
            v2_failure.py app.py dispatcher.py lifecycle_worker.py \
            grt_web_common.py runner_lifecycle_authority.py \
            cli_create_authority.py pty_token_adapter.py; do
  [[ -f "$ROOT/web/$file" && ! -L "$ROOT/web/$file" ]] ||
    { echo "Missing fixed V2 module $file" >&2; exit 1; }
  sudo install -o root -g root -m 0755 "$ROOT/web/$file" "$INSTALL_ROOT/$file"
done
# V2 has one pinned CLI authority under /usr/local/lib/github-runner-tools/cli.
# Keep the old V1 Web CLI files available only for an explicitly authorized
# rollback. Never allow either grt-web or actions to write the V2 CLI root.
if sudo test -L "$CLI_ROOT"; then
  echo "Symlinked V2 CLI directory refused" >&2; exit 1
fi
if sudo test -e "$CLI_ROOT"; then
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$CLI_ROOT")" == "root:root:755:directory" ]] ||
    { echo "Unexpected CLI directory owner or mode" >&2; exit 1; }
else
  sudo install -d -o root -g root -m 0755 "$CLI_ROOT"
fi
for file in register-runner.sh remove-runner.sh status-runners.sh \
            web-service-record.py web-runner-cleanup.py; do
  [[ -f "$ROOT/scripts/$file" && ! -L "$ROOT/scripts/$file" ]] ||
    { echo "Missing fixed CLI artifact $file" >&2; exit 1; }
  sudo install -o root -g root -m 0755 "$ROOT/scripts/$file" "$CLI_ROOT/$file"
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
