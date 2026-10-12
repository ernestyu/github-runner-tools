#!/usr/bin/env bash
set -Eeuo pipefail

# Fresh-host installer for the current CLI + Web/Executor architecture.
# This script is executed only from a release payload whose SHA-256 was
# verified by the release bootstrap.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
STATE_ROOT="/var/lib/github-runner-tools"
INSTALL_STATE="$STATE_ROOT/install-state.json"
INSTALL_ROOT="/usr/local/lib/github-runner-tools"
CLI_ROOT="$INSTALL_ROOT/cli"
WEB_ROOT="$INSTALL_ROOT/web"
CONFIG_ROOT="/etc/github-runner-tools"
WEB_CONFIG="$CONFIG_ROOT/web.conf"
WEB_AUTH="$CONFIG_ROOT/web-auth.conf"
EXEC_UNIT="/etc/systemd/system/github-runner-tools-executor.service"
WEB_UNIT="/etc/systemd/system/github-runner-tools-web.service"
LOG_ROOT="/var/log/github-runner-tools"
LOCK_ROOT="/run/lock/github-runner-tools"
LOCK_FILE="$LOCK_ROOT/mutation.lock"
WEB_USER="grt-web"
WEB_PORT="8765"
RELEASE="${GRT_RELEASE_VERSION:-}"
PAYLOAD_SHA256="${GRT_PAYLOAD_SHA256:-}"

die() { echo "INSTALL_FAILED stage=${CURRENT_STAGE:-PREFLIGHT} reason=$1" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing_command_$1"; }

# Security boundary: never infer SUDO_USER from a root invocation.
if [[ "${EUID}" -eq 0 ]]; then
  echo "INSTALL_FAILED stage=PREFLIGHT reason=root_invocation_forbidden" >&2
  exit 1
fi

for cmd in bash sudo id getent ps xargs ip install stat grep sed awk mktemp flock sha256sum tar; do need "$cmd"; done
[[ "$RELEASE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "invalid_release_binding"
[[ "$PAYLOAD_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "invalid_payload_binding"
[[ -f "$ROOT/web/app.py" && -f "$ROOT/scripts/register-runner.sh" ]] || die "incomplete_verified_payload"

RUNNER_USER="$(id -un)"
RUNNER_GROUP="$(id -gn)"
RUNNER_UID="$(id -u)"
RUNNER_GID="$(id -g)"
RUNNER_HOME="$(getent passwd "$RUNNER_USER" | awk -F: 'NR==1 {print $6}')"
[[ "$RUNNER_UID" -gt 0 && "$RUNNER_GID" -gt 0 ]] || die "runner_identity_must_be_nonroot"
[[ "$RUNNER_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || die "unsupported_runner_username"
[[ "$RUNNER_HOME" =~ ^/[A-Za-z0-9._/-]+$ && -d "$RUNNER_HOME" ]] || die "unsupported_runner_home"
[[ "$(ps -p 1 -o comm= 2>/dev/null | xargs)" == systemd ]] || die "systemd_required"

sudo -v || die "sudo_required"

source "$ROOT/scripts/install-lan.sh"


managed_object_exists_without_state() {
  local p
  for p in "$INSTALL_ROOT" "$CONFIG_ROOT" "$LOG_ROOT" "$EXEC_UNIT" "$WEB_UNIT"; do
    if sudo test -e "$p" || sudo test -L "$p"; then return 0; fi
  done
  if sudo test -d "$STATE_ROOT"; then
    # Empty state root is still an unexplained partial installation.
    return 0
  fi
  return 1
}

state_value() {
  local key="$1"
  sudo sed -n "s/.*\"${key}\":\"\([^\"]*\)\".*/\1/p" "$INSTALL_STATE" | head -n1
}
state_number() {
  local key="$1"
  sudo sed -n "s/.*\"${key}\":\([0-9][0-9]*\).*/\1/p" "$INSTALL_STATE" | head -n1
}

write_state() {
  local stage="$1" tmp
  tmp="$(mktemp)"
  cat >"$tmp" <<EOF
{"schema":1,"release":"$RELEASE","payload_sha256":"$PAYLOAD_SHA256","attempt_id":"$ATTEMPT_ID","runner_user":"$RUNNER_USER","runner_uid":$RUNNER_UID,"runner_gid":$RUNNER_GID,"runner_home":"$RUNNER_HOME","web_bind_address":"$WEB_BIND_ADDRESS","stage":"$stage"}
EOF
  sudo install -o root -g root -m 0600 "$tmp" "$STATE_ROOT/.install-state.json.$$"
  sudo mv -f "$STATE_ROOT/.install-state.json.$$" "$INSTALL_STATE"
  rm -f "$tmp"
  CURRENT_STAGE="$stage"
}

stage_rank() {
  case "$1" in
    ATTEMPT_STARTED) echo 1 ;;
    RUNTIME_INSTALLED) echo 2 ;;
    AUTHORITY_STATE_INITIALIZED) echo 3 ;;
    ARCHIVE_PLATFORM_INITIALIZED) echo 4 ;;
    EXECUTOR_INSTALLED) echo 5 ;;
    WEB_INSTALLED) echo 6 ;;
    SERVICES_STARTED) echo 7 ;;
    SMOKE_VERIFIED) echo 8 ;;
    INSTALL_COMPLETE) echo 9 ;;
    *) return 1 ;;
  esac
}
stage_at_least() {
  local have want
  have="$(stage_rank "$CURRENT_STAGE")" || die "unknown_install_stage"
  want="$(stage_rank "$1")" || die "invalid_internal_stage"
  (( have >= want ))
}
fault_after() {
  # Test-only failure injection can only stop progress; it cannot bypass checks.
  if [[ "${GRT_TEST_ALLOW_FAULTS:-0}" == 1 && "${GRT_TEST_FAULT_STAGE:-}" == "$1" ]]; then
    die "injected_fault"
  fi
}

verify_file() {
  local src="$1" dst="$2" mode="$3"
  sudo test -f "$dst" && ! sudo test -L "$dst" || die "managed_file_missing"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$dst")" == "root:root:$mode:regular file" ]] || die "managed_file_metadata_mismatch"
  sudo cmp -s "$src" "$dst" || die "managed_file_content_mismatch"
}

WEB_BIND_ADDRESS=""
ATTEMPT_ID=""
CURRENT_STAGE="PREFLIGHT"

if sudo test -e "$INSTALL_STATE" || sudo test -L "$INSTALL_STATE"; then
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$INSTALL_STATE")" == "root:root:600:regular file" ]] ||
    die "unsafe_install_state"
  [[ "$(state_number schema)" == 1 ]] || die "invalid_install_state_schema"
  [[ "$(state_value release)" == "$RELEASE" ]] || die "partial_release_mismatch"
  [[ "$(state_value payload_sha256)" == "$PAYLOAD_SHA256" ]] || die "partial_payload_mismatch"
  [[ "$(state_value runner_user)" == "$RUNNER_USER" ]] || die "partial_runner_user_mismatch"
  [[ "$(state_number runner_uid)" == "$RUNNER_UID" ]] || die "partial_runner_uid_mismatch"
  [[ "$(state_number runner_gid)" == "$RUNNER_GID" ]] || die "partial_runner_gid_mismatch"
  [[ "$(state_value runner_home)" == "$RUNNER_HOME" ]] || die "partial_runner_home_mismatch"
  ATTEMPT_ID="$(state_value attempt_id)"
  WEB_BIND_ADDRESS="$(state_value web_bind_address)"
  CURRENT_STAGE="$(state_value stage)"
  [[ "$ATTEMPT_ID" =~ ^[0-9a-fA-F-]{16,64}$ ]] || die "invalid_attempt_identity"
  stage_rank "$CURRENT_STAGE" >/dev/null || die "unknown_install_stage"
  [[ "$CURRENT_STAGE" != INSTALL_COMPLETE ]] || die "installation_already_complete"
else
  managed_object_exists_without_state && die "untracked_partial_installation"
  WEB_BIND_ADDRESS="$(grt_select_lan_address)" || die "lan_address_selection_failed"
  ATTEMPT_ID="$(cat /proc/sys/kernel/random/uuid)"
  sudo install -d -o root -g root -m 0700 "$STATE_ROOT"
  write_state ATTEMPT_STARTED
fi

# Persisted bind address is authoritative; never rediscover on retry.
ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$WEB_BIND_ADDRESS" ||
  die "configured_lan_address_not_assigned"

if stage_at_least RUNTIME_INSTALLED; then
  for f in register-runner.sh remove-runner.sh status-runners.sh cli-operational-logger.py web-runner-cleanup.py web-service-record.py; do
    verify_file "$ROOT/scripts/$f" "$CLI_ROOT/$f" 755
  done
  for src in "$ROOT"/web/*.py; do verify_file "$src" "$WEB_ROOT/${src##*/}" 755; done
else
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 jq rsync curl ca-certificates
  sudo install -d -o root -g root -m 0755 "$CLI_ROOT" "$WEB_ROOT"
  for f in register-runner.sh remove-runner.sh status-runners.sh cli-operational-logger.py web-runner-cleanup.py web-service-record.py; do
    sudo install -o root -g root -m 0755 "$ROOT/scripts/$f" "$CLI_ROOT/$f"
  done
  for src in "$ROOT"/web/*.py; do
    sudo install -o root -g root -m 0755 "$src" "$WEB_ROOT/${src##*/}"
  done
  write_state RUNTIME_INSTALLED
  fault_after RUNTIME_INSTALLED
fi

if stage_at_least AUTHORITY_STATE_INITIALIZED; then
  getent passwd "$WEB_USER" >/dev/null || die "web_user_missing"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$STATE_ROOT")" == "root:root:700:directory" ]] || die "state_root_mismatch"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$LOCK_ROOT")" == "root:root:755:directory" ]] || die "lock_root_mismatch"
  [[ "$(sudo stat -c '%u:%g:%a:%F' "$LOCK_FILE")" == "0:$RUNNER_GID:660:regular file" ]] || die "lock_file_mismatch"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$LOG_ROOT")" == "root:$RUNNER_GROUP:2770:directory" ]] || die "log_root_mismatch"
else
  if ! getent passwd "$WEB_USER" >/dev/null; then
    sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$WEB_USER"
  fi
  [[ "$(getent passwd "$WEB_USER" | cut -d: -f3)" -gt 0 ]] || die "invalid_web_user"
  sudo install -d -o root -g root -m 0755 "$LOCK_ROOT"
  if ! sudo test -e "$LOCK_FILE"; then
    sudo install -o root -g "$RUNNER_GROUP" -m 0660 /dev/null "$LOCK_FILE"
  fi
  sudo install -d -o root -g "$RUNNER_GROUP" -m 2770 "$LOG_ROOT"
  if ! sudo test -e "$LOG_ROOT/operational.lock"; then
    sudo install -o "$RUNNER_USER" -g "$RUNNER_GROUP" -m 0600 /dev/null "$LOG_ROOT/operational.lock"
  fi
  if ! sudo test -e "$LOG_ROOT/operational.jsonl"; then
    sudo install -o "$RUNNER_USER" -g "$RUNNER_GROUP" -m 0600 /dev/null "$LOG_ROOT/operational.jsonl"
  fi
  write_state AUTHORITY_STATE_INITIALIZED
  fault_after AUTHORITY_STATE_INITIALIZED
fi

if stage_at_least ARCHIVE_PLATFORM_INITIALIZED; then
  sudo grep -Fqx '# managed-by=github-runner-tools' "$CONFIG_ROOT/archive.conf" || die "archive_config_mismatch"
  verify_file "$ROOT/hooks/archive-job-completed.sh" "$INSTALL_ROOT/hooks/archive-job-completed.sh" 755
else
  RUNNER_ARCHIVE_ROOT="${RUNNER_ARCHIVE_ROOT:-/srv/github-actions-archive}"     bash "$ROOT/scripts/setup-local-archive.sh" --apply
  write_state ARCHIVE_PLATFORM_INITIALIZED
  fault_after ARCHIVE_PLATFORM_INITIALIZED
fi

make_executor_unit() {
  cat <<EOF
[Unit]
Description=github-runner-tools Executor
After=network-online.target systemd-tmpfiles-setup.service
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
RuntimeDirectory=github-runner-tools
RuntimeDirectoryMode=0755
ExecStart=/usr/bin/python3 $WEB_ROOT/v2_executor.py
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
ReadWritePaths=$RUNNER_HOME /etc/systemd/system /run/github-runner-tools $LOCK_ROOT $STATE_ROOT $LOG_ROOT

[Install]
WantedBy=multi-user.target
EOF
}
make_web_unit() {
  cat <<EOF
[Unit]
Description=github-runner-tools Web
After=network-online.target github-runner-tools-executor.service
Wants=network-online.target
Requires=github-runner-tools-executor.service

[Service]
Type=simple
User=$WEB_USER
Group=$WEB_USER
ExecStart=/usr/bin/python3 $WEB_ROOT/app.py --config $WEB_CONFIG --auth-config $WEB_AUTH
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
}
verify_generated() {
  local dst="$1" generator="$2" tmp
  tmp="$(mktemp)"; "$generator" >"$tmp"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$dst")" == "root:root:644:regular file" ]] || { rm -f "$tmp"; die "unit_metadata_mismatch"; }
  sudo cmp -s "$tmp" "$dst" || { rm -f "$tmp"; die "unit_content_mismatch"; }
  rm -f "$tmp"
}

if stage_at_least EXECUTOR_INSTALLED; then
  verify_generated "$EXEC_UNIT" make_executor_unit
else
  tmp="$(mktemp)"; make_executor_unit >"$tmp"
  sudo install -o root -g root -m 0644 "$tmp" "$EXEC_UNIT"; rm -f "$tmp"
  write_state EXECUTOR_INSTALLED
  fault_after EXECUTOR_INSTALLED
fi

if stage_at_least WEB_INSTALLED; then
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$WEB_CONFIG")" == "root:$WEB_USER:640:regular file" ]] || die "web_config_mismatch"
  sudo grep -Fqx "WEB_BIND_ADDRESS=$WEB_BIND_ADDRESS" "$WEB_CONFIG" || die "web_bind_mismatch"
  sudo grep -Fqx 'WEB_COOKIE_SECURE=0' "$WEB_CONFIG" || die "web_cookie_mode_mismatch"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$WEB_AUTH")" == "root:$WEB_USER:640:regular file" ]] || die "web_auth_mismatch"
  sudo grep -Eq '^PASSWORD_HASH=scrypt\$' "$WEB_AUTH" || die "web_auth_invalid"
  verify_generated "$WEB_UNIT" make_web_unit
else
  sudo install -d -o root -g root -m 0755 "$CONFIG_ROOT"
  cfg="$(mktemp)"
  cat >"$cfg" <<EOF
RUNNER_USER=$RUNNER_USER
RUNNER_GROUP=$RUNNER_GROUP
RUNNER_HOME=$RUNNER_HOME
WEB_USER=$WEB_USER
WEB_BIND_ADDRESS=$WEB_BIND_ADDRESS
WEB_PORT=$WEB_PORT
WEB_COOKIE_SECURE=0
MUTATION_LOCK_FILE=$LOCK_FILE
MUTATION_TIMEOUT_SECONDS=900
EOF
  sudo install -o root -g "$WEB_USER" -m 0640 "$cfg" "$WEB_CONFIG"; rm -f "$cfg"

  [[ -r /dev/tty && -w /dev/tty ]] || die "administrator_password_requires_tty"
  printf 'Web administrator password: ' >/dev/tty
  IFS= read -r -s password </dev/tty; printf '\n' >/dev/tty
  printf 'Confirm Web administrator password: ' >/dev/tty
  IFS= read -r -s confirm </dev/tty; printf '\n' >/dev/tty
  [[ -n "$password" && "$password" == "$confirm" ]] || { password= confirm=; die "administrator_password_mismatch"; }
  auth_tmp="$(mktemp)"
  PYTHONPATH="$ROOT/web" /usr/bin/python3 - "$password" >"$auth_tmp" <<'PY'
import sys
from grt_web_common import password_hash
print("PASSWORD_HASH=" + password_hash(sys.argv[1]))
PY
  password= confirm=
  sudo install -o root -g "$WEB_USER" -m 0640 "$auth_tmp" "$WEB_AUTH"; rm -f "$auth_tmp"
  unit_tmp="$(mktemp)"; make_web_unit >"$unit_tmp"
  sudo install -o root -g root -m 0644 "$unit_tmp" "$WEB_UNIT"; rm -f "$unit_tmp"
  write_state WEB_INSTALLED
  fault_after WEB_INSTALLED
fi

if stage_at_least SERVICES_STARTED; then
  sudo systemctl is-active --quiet github-runner-tools-executor.service || die "executor_not_active"
  sudo systemctl is-active --quiet github-runner-tools-web.service || die "web_not_active"
else
  sudo systemctl daemon-reload
  sudo systemctl enable --now github-runner-tools-executor.service
  sudo systemctl enable --now github-runner-tools-web.service
  write_state SERVICES_STARTED
  fault_after SERVICES_STARTED
fi

if stage_at_least SMOKE_VERIFIED; then
  :
else
  sudo systemctl is-active --quiet github-runner-tools-executor.service || die "executor_smoke_failed"
  sudo systemctl is-active --quiet github-runner-tools-web.service || die "web_smoke_failed"
  sudo test -S /run/github-runner-tools/executor.sock || die "executor_socket_missing"
  web_gid="$(getent group "$WEB_USER" | cut -d: -f3)"
  [[ "$(sudo stat -c '%u:%g:%a:%F' /run/github-runner-tools/executor.sock)" == "0:$web_gid:660:socket" ]] || die "executor_socket_mismatch"
  "$CLI_ROOT/status-runners.sh" --json >/dev/null || die "cli_status_smoke_failed"
  sudo -u "$WEB_USER" /usr/bin/python3 -c "import sys; sys.path.insert(0,'$WEB_ROOT'); from v2_executor_client import web_list; r=web_list(); raise SystemExit(0 if r.get('ok') else 1)" ||
    die "executor_status_smoke_failed"
  tail_line="$(sudo tail -n 1 "$LOG_ROOT/operational.jsonl")"
  [[ "$tail_line" == *'"event":"operation_exit"'* && "$tail_line" == *'"op":"status"'* && "$tail_line" == *'"exit_code":0'* ]] ||
    die "operational_log_smoke_failed"
  write_state SMOKE_VERIFIED
  fault_after SMOKE_VERIFIED
fi

write_state INSTALL_COMPLETE

cat <<EOF

github-runner-tools installation complete.
Runner owner : $RUNNER_USER
Web URL      : http://$WEB_BIND_ADDRESS:$WEB_PORT/

LAN HTTP is unencrypted. Use this endpoint only on a trusted home/lab/
management LAN. Do not expose it directly to the public Internet.
EOF
