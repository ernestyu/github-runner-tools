#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
APPLY=0
WEB_USER="grt-web"
WEB_PORT="8765"
INSTALL_ROOT="/usr/local/lib/github-runner-tools/web"
CONFIG_DIR="/etc/github-runner-tools"
CONFIG_FILE="$CONFIG_DIR/web.conf"
AUTH_FILE="$CONFIG_DIR/web-auth.conf"
DISPATCH_SOCKET="/run/github-runner-tools/web-dispatch.sock"
LOCK_DIR="/run/lock/github-runner-tools"
LOCK_FILE="$LOCK_DIR/mutation.lock"
MANAGED_MARKER="# managed-by=github-runner-tools-web-v1"
INSTALL_MARKER="$INSTALL_ROOT/.managed-by-github-runner-tools-web-v1"
TMPFILES_CONFIG="/etc/tmpfiles.d/github-runner-tools-web.conf"

die() { echo "ERROR: $*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

usage() {
  cat <<'USAGE'
Usage:
  setup-web-management.sh [--dry-run|--apply]

Default: --dry-run

Web Management is optional. This script is the only installation entry point
for the Web component. Existing CLI/archive commands never enable it.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) APPLY=0; shift ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ ${EUID} -ne 0 ]] || die "Run this script as the normal runner owner, not root."
for cmd in python3 sudo id getent systemctl systemd-tmpfiles tailscale install stat awk ps xargs useradd mktemp; do require_command "$cmd"; done

RUNNER_USER="$(id -un)"
RUNNER_GROUP="$(id -gn)"
RUNNER_UID="$(id -u)"
RUNNER_GID="$(id -g)"
RUNNER_HOME="$(getent passwd "$RUNNER_USER" | awk -F: 'NR==1 {print $6}')"
[[ -n "$RUNNER_HOME" && "$RUNNER_HOME" = /* && -d "$RUNNER_HOME" ]] || die "Could not resolve runner home."

PID1="$(ps -p 1 -o comm= 2>/dev/null | xargs)"
[[ "$PID1" == "systemd" ]] || die "Web Management V1 requires systemd."
systemctl is-active --quiet tailscaled || die "tailscaled is not active."

cat <<PLAN
Web Management V1 setup plan

Mode             : $([[ "$APPLY" == "1" ]] && echo APPLY || echo DRY-RUN)
Runner user      : $RUNNER_USER ($RUNNER_UID:$RUNNER_GID)
Runner home      : $RUNNER_HOME
Web user         : $WEB_USER
Backend          : 127.0.0.1:$WEB_PORT
Install root     : $INSTALL_ROOT
Config           : $CONFIG_FILE
Auth config      : $AUTH_FILE
Dispatch socket  : $DISPATCH_SOCKET
Shared lock      : $LOCK_FILE

This is an explicit opt-in component.
CLI-only operation remains supported without applying this setup.
PLAN

if [[ "$APPLY" != "1" ]]; then
  echo
  echo "DRY-RUN: no persistent host mutation performed."
  echo "Would install root-owned Web/dispatcher/worker code and systemd units."
  echo "Would NOT enable a public listener or Tailscale Funnel."
  exit 0
fi

sudo -v || die "sudo access is required for explicit Web setup."

# Refuse to replace unmanaged Web components.
for managed_file in "$CONFIG_FILE" "$AUTH_FILE"   /etc/systemd/system/github-runner-tools-web.service   /etc/systemd/system/github-runner-tools-dispatch.service   "$TMPFILES_CONFIG"; do
  if sudo test -L "$managed_file"; then
    die "Refusing symlinked Web Management file: $managed_file"
  fi
  if sudo test -e "$managed_file" && ! sudo grep -Fqx "$MANAGED_MARKER" "$managed_file" 2>/dev/null; then
    die "Refusing to replace unmanaged Web Management file: $managed_file"
  fi
done

if sudo test -L "$INSTALL_ROOT"; then
  die "Refusing symlinked Web install directory: $INSTALL_ROOT"
fi
if sudo test -e "$INSTALL_ROOT"; then
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$INSTALL_ROOT")" == "root:root:755:directory" ]] ||
    die "Existing Web install directory has unexpected ownership/mode/type."
  sudo test -f "$INSTALL_MARKER" ||
    die "Refusing to replace unmanaged Web install directory: $INSTALL_ROOT"
  sudo grep -Fqx "$MANAGED_MARKER" "$INSTALL_MARKER" ||
    die "Web install directory marker is invalid."
fi

# The shared lock is core lifecycle infrastructure. Web setup reuses the same
# root-controlled path and does not create a Web-specific lock authority.
if sudo test -L "$LOCK_DIR"; then die "Shared mutation lock directory must not be a symlink."; fi
if sudo test -e "$LOCK_DIR"; then
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$LOCK_DIR")" == "root:root:755:directory" ]] ||
    die "Existing shared mutation lock directory has unexpected ownership/mode/type."
else
  sudo install -d -o root -g root -m 0755 "$LOCK_DIR"
fi

if sudo test -L "$LOCK_FILE"; then die "Shared mutation lock must not be a symlink."; fi
if sudo test -e "$LOCK_FILE"; then
  LOCK_META="$(sudo stat -c '%U:%G:%a:%F' "$LOCK_FILE")"
  [[ "$LOCK_META" == "root:$RUNNER_GROUP:660:regular empty file" ||
     "$LOCK_META" == "root:$RUNNER_GROUP:660:regular file" ]] ||
    die "Existing shared mutation lock has unexpected ownership/mode/type."
else
  sudo touch "$LOCK_FILE"
  sudo chown root:"$RUNNER_GROUP" "$LOCK_FILE"
  sudo chmod 0660 "$LOCK_FILE"
fi

TMP_TMPFILES="$(mktemp)"
cat > "$TMP_TMPFILES" <<TMPFILES
$MANAGED_MARKER
d /run/lock/github-runner-tools 0755 root root -
f /run/lock/github-runner-tools/mutation.lock 0660 root $RUNNER_GROUP -
TMPFILES
sudo install -o root -g root -m 0644 "$TMP_TMPFILES" "$TMPFILES_CONFIG"
rm -f -- "$TMP_TMPFILES"
sudo systemd-tmpfiles --create "$TMPFILES_CONFIG"

if ! getent passwd "$WEB_USER" >/dev/null; then
  sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$WEB_USER"
fi
WEB_UID="$(id -u "$WEB_USER")"
WEB_GID="$(id -g "$WEB_USER")"
[[ "$WEB_UID" != "$RUNNER_UID" ]] || die "Web user must differ from runner owner."

sudo install -d -o root -g root -m 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/cli"
TMP_MARKER="$(mktemp)"
printf '%s\n' "$MANAGED_MARKER" > "$TMP_MARKER"
sudo install -o root -g root -m 0644 "$TMP_MARKER" "$INSTALL_MARKER"
rm -f -- "$TMP_MARKER"
for file in grt_web_common.py app.py dispatcher.py lifecycle_worker.py pty_token_adapter.py runner_lifecycle_authority.py; do
  [[ -f "$ROOT/web/$file" ]] || die "Missing source file: web/$file"
  sudo install -o root -g root -m 0755 "$ROOT/web/$file" "$INSTALL_ROOT/$file"
done
for file in register-runner.sh remove-runner.sh status-runners.sh; do
  sudo install -o root -g root -m 0755 "$ROOT/scripts/$file" "$INSTALL_ROOT/cli/$file"
done
sudo install -o root -g root -m 0755 "$ROOT/scripts/web-service-record.py" "$INSTALL_ROOT/cli/web-service-record.py"
sudo install -o root -g root -m 0755 "$ROOT/scripts/web-runner-cleanup.py" "$INSTALL_ROOT/cli/web-runner-cleanup.py"

if sudo test -L "$CONFIG_DIR"; then die "Configuration directory must not be a symlink."; fi
if sudo test -e "$CONFIG_DIR"; then
  [[ "$(sudo stat -c '%U:%a:%F' "$CONFIG_DIR")" == "root:755:directory" ]] ||
    die "Existing configuration directory must be root-owned mode 0755; refusing to change shared archive configuration access."
else
  sudo install -d -o root -g root -m 0755 "$CONFIG_DIR"
fi
TMP_CONFIG="$(mktemp)"
cat > "$TMP_CONFIG" <<CFG
$MANAGED_MARKER
WEB_BIND_ADDRESS=127.0.0.1
WEB_PORT=$WEB_PORT
WEB_USER=$WEB_USER
RUNNER_USER=$RUNNER_USER
RUNNER_GROUP=$RUNNER_GROUP
RUNNER_HOME=$RUNNER_HOME
MUTATION_TIMEOUT_SECONDS=900
DISPATCH_SOCKET=$DISPATCH_SOCKET
MUTATION_LOCK_FILE=$LOCK_FILE
WORKER_PATH=$INSTALL_ROOT/lifecycle_worker.py
CLI_DIR=$INSTALL_ROOT/cli
PTY_ADAPTER=$INSTALL_ROOT/pty_token_adapter.py
CFG
sudo install -o root -g "$WEB_USER" -m 0640 "$TMP_CONFIG" "$CONFIG_FILE"
rm -f -- "$TMP_CONFIG"

if [[ ! -f "$AUTH_FILE" ]]; then
  [[ -r /dev/tty && -w /dev/tty ]] || die "Password setup requires a TTY."
  IFS= read -r -s -p "Choose Web administrator password: " PASSWORD < /dev/tty
  printf '\n' > /dev/tty
  IFS= read -r -s -p "Repeat Web administrator password: " PASSWORD2 < /dev/tty
  printf '\n' > /dev/tty
  [[ -n "$PASSWORD" && "$PASSWORD" == "$PASSWORD2" ]] || { unset PASSWORD PASSWORD2; die "Passwords do not match or are empty."; }
  PASSWORD_HASH="$(printf '%s' "$PASSWORD" | PYTHONPATH="$ROOT/web" python3 -c 'import sys; from grt_web_common import password_hash; print(password_hash(sys.stdin.read()))')"
  unset PASSWORD PASSWORD2
  TMP_AUTH="$(mktemp)"
  {
    printf '%s\n' "$MANAGED_MARKER"
    printf 'PASSWORD_HASH=%s\n' "$PASSWORD_HASH"
  } > "$TMP_AUTH"
  unset PASSWORD_HASH
  sudo install -o root -g "$WEB_USER" -m 0640 "$TMP_AUTH" "$AUTH_FILE"
  rm -f -- "$TMP_AUTH"
else
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$AUTH_FILE")" == "root:$WEB_USER:640:regular file" ]] ||
    die "Existing Web authentication config has unexpected ownership/mode/type."
  echo "==> Preserving existing Web authentication config: $AUTH_FILE"
fi

WEB_UNIT="/etc/systemd/system/github-runner-tools-web.service"
DISPATCH_UNIT="/etc/systemd/system/github-runner-tools-dispatch.service"

TMP_WEB_UNIT="$(mktemp)"
cat > "$TMP_WEB_UNIT" <<UNIT
$MANAGED_MARKER
[Unit]
Description=github-runner-tools Web Management
After=network-online.target tailscaled.service github-runner-tools-dispatch.service
Wants=network-online.target
Requires=github-runner-tools-dispatch.service

[Service]
Type=simple
User=$WEB_USER
Group=$WEB_USER
ExecStart=/usr/bin/python3 $INSTALL_ROOT/app.py --config $CONFIG_FILE --auth-config $AUTH_FILE
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
UNIT

TMP_DISPATCH_UNIT="$(mktemp)"
cat > "$TMP_DISPATCH_UNIT" <<UNIT
$MANAGED_MARKER
[Unit]
Description=github-runner-tools privileged Web dispatcher
After=network-online.target systemd-tmpfiles-setup.service
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
RuntimeDirectory=github-runner-tools
RuntimeDirectoryMode=0755
ExecStart=/usr/bin/python3 $INSTALL_ROOT/dispatcher.py --config $CONFIG_FILE
Restart=on-failure
RestartSec=2
# The dispatcher must launch the fixed worker as root and the worker must then
# irreversibly drop to the runner owner. Under NoNewPrivileges=yes, do not rely
# on implicit root exec semantics to retain CAP_SETUID/CAP_SETGID across exec.
# Provision exactly these identity-transition capabilities explicitly.
AmbientCapabilities=CAP_SETUID CAP_SETGID
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=$RUNNER_HOME /etc/systemd/system /run/github-runner-tools /run/lock/github-runner-tools

[Install]
WantedBy=multi-user.target
UNIT

sudo install -o root -g root -m 0644 "$TMP_WEB_UNIT" "$WEB_UNIT"
sudo install -o root -g root -m 0644 "$TMP_DISPATCH_UNIT" "$DISPATCH_UNIT"
rm -f -- "$TMP_WEB_UNIT" "$TMP_DISPATCH_UNIT"

for installed in "$INSTALL_ROOT"/*.py "$INSTALL_ROOT"/cli/*.sh "$INSTALL_ROOT/cli/web-service-record.py" "$INSTALL_ROOT/cli/web-runner-cleanup.py"; do
  [[ "$(sudo stat -c '%U:%a' "$installed")" == root:* ]] || die "Installed code is not root-owned: $installed"
  mode="$(sudo stat -c '%a' "$installed")"
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || die "Installed code has invalid mode: $installed"
  (( (8#$mode & 8#022) == 0 )) || die "Installed code is group/world writable: $installed"
done

sudo systemctl daemon-reload
sudo systemctl enable github-runner-tools-dispatch.service
sudo systemctl enable github-runner-tools-web.service
sudo systemctl restart github-runner-tools-dispatch.service
sudo systemctl restart github-runner-tools-web.service

echo
echo "Web Management installed and enabled."
echo "Backend is loopback-only: http://127.0.0.1:$WEB_PORT"
echo
echo "Tailscale Serve is NOT configured automatically."
echo "After reviewing your Tailnet policy, expose the loopback backend with:"
echo
echo "  sudo tailscale serve --bg http://127.0.0.1:$WEB_PORT"
echo
echo "Do not use 'tailscale funnel'. Direct LAN/WAN access is unsupported in V1."
