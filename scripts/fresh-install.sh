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
AUTH_RECOVERY="$STATE_ROOT/web-auth-recovery.json"
EXEC_UNIT="/etc/systemd/system/github-runner-tools-executor.service"
WEB_UNIT="/etc/systemd/system/github-runner-tools-web.service"
LOG_ROOT="/var/log/github-runner-tools"
LOCK_ROOT="/run/lock/github-runner-tools"
LOCK_FILE="$LOCK_ROOT/mutation.lock"
WEB_USER="grt-web"
WEB_PORT="8765"
RELEASE="${GRT_RELEASE_VERSION:-}"
PAYLOAD_SHA256="${GRT_PAYLOAD_SHA256:-}"

die() { echo "INSTALL_FAILED stage=${ACTIVE_STAGE:-PREFLIGHT} last_completed=${CURRENT_STAGE:-NONE} reason=$1" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing_command_$1"; }

# Security boundary: never infer SUDO_USER from a root invocation.
if [[ "${EUID}" -eq 0 ]]; then
  echo "INSTALL_FAILED stage=PREFLIGHT reason=root_invocation_forbidden" >&2
  exit 1
fi

for cmd in bash sudo id getent ps xargs ip install stat grep sed awk mktemp flock sha256sum tar python3; do need "$cmd"; done
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
[[ -r /etc/os-release ]] || die "os_release_unavailable"
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "debian" && "${VERSION_ID:-}" == "13" ]] || die "debian_13_required"
case "$(uname -m)" in
  x86_64|aarch64) ;;
  *) die "unsupported_architecture" ;;
esac

sudo -v || die "sudo_required"

source "$ROOT/scripts/install-lan.sh"


managed_object_exists_without_state() {
  local p
  for p in "$INSTALL_ROOT" "$CONFIG_ROOT" "$LOG_ROOT" "$EXEC_UNIT" "$WEB_UNIT"; do
    if sudo test -e "$p" || sudo test -L "$p"; then return 0; fi
  done
  if sudo test -d "$STATE_ROOT"; then
    # The first committed installer object is install-state.json. If the
    # process died between creating its parent and publishing that record,
    # accept only the exact empty root-owned 0700 directory. Any content or
    # metadata mismatch remains an unknown partial installation.
    if [[ "$(sudo stat -c '%U:%G:%a:%F' "$STATE_ROOT")" == "root:root:700:directory" ]] &&
       [[ -z "$(sudo find "$STATE_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      return 1
    fi
    return 0
  fi
  return 1
}

# Strict state validation is performed before accepting any persisted binding.
read_install_state() {
  local fields
  fields="$(sudo /usr/bin/python3 "$ROOT/scripts/install-state-validator.py" "$STATE_ROOT" install-state.json)" ||
    die "invalid_install_state"
  mapfile -t INSTALL_FIELDS <<<"$fields"
  [[ "${#INSTALL_FIELDS[@]}" == 10 ]] || die "invalid_install_state"
}

write_state() {
  local stage="$1" tmp
  tmp="$(mktemp)"
  cat >"$tmp" <<EOF
{"schema":1,"release":"$RELEASE","payload_sha256":"$PAYLOAD_SHA256","attempt_id":"$ATTEMPT_ID","runner_user":"$RUNNER_USER","runner_uid":$RUNNER_UID,"runner_gid":$RUNNER_GID,"runner_home":"$RUNNER_HOME","web_bind_address":"$WEB_BIND_ADDRESS","stage":"$stage"}
EOF
  sudo install -o root -g root -m 0600 "$tmp" "$STATE_ROOT/.install-state.json.${BASHPID}"
  sudo mv -f "$STATE_ROOT/.install-state.json.${BASHPID}" "$INSTALL_STATE"
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
fault_inside() {
  if [[ "${GRT_TEST_ALLOW_FAULTS:-0}" == 1 && "${GRT_TEST_FAULT_INSIDE:-}" == "$1" ]]; then
    die "injected_inside_fault"
  fi
}
checked_install() {
  local src="$1" dst="$2" owner="$3" group="$4" mode="$5"
  if sudo test -e "$dst" || sudo test -L "$dst"; then
    sudo test -f "$dst" && ! sudo test -L "$dst" || die "partial_file_type_mismatch"
    [[ "$(sudo stat -c '%U:%G:%a:%h' "$dst")" == "$owner:$group:$mode:1" ]] || die "partial_file_metadata_mismatch"
    sudo cmp -s "$src" "$dst" || die "partial_file_content_mismatch"
  else
    sudo install -o "$owner" -g "$group" -m "$mode" "$src" "$dst"
  fi
}
checked_directory() {
  local dst="$1" owner="$2" group="$3" mode="$4"
  if sudo test -e "$dst" || sudo test -L "$dst"; then
    verify_directory "$dst" "$owner" "$group" "$mode"
  else
    sudo install -d -o "$owner" -g "$group" -m "$mode" "$dst"
  fi
}
check_entries_subset() {
  local dst="$1" entry expected allowed
  shift
  sudo test -d "$dst" || return 0
  while IFS= read -r entry; do
    allowed=0
    for expected in "$@"; do [[ "$entry" == "$expected" ]] && allowed=1; done
    [[ "$allowed" == 1 ]] || die "unknown_managed_object"
  done < <(sudo find "$dst" -mindepth 1 -maxdepth 1 -printf '%f\\n')
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
  [[ "$(sudo stat -c '%U:%G:%a' "$dst")" == "root:root:$mode" ]] || die "managed_file_metadata_mismatch"
  sudo cmp -s "$src" "$dst" || die "managed_file_content_mismatch"
}

verify_directory() {
  local path="$1" owner="$2" group="$3" mode="$4"
  sudo test -d "$path" && ! sudo test -L "$path" || die "managed_directory_metadata_mismatch"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$path")" == "$owner:$group:$mode:directory" ]] ||
    die "managed_directory_metadata_mismatch"
}

verify_exact_entries() {
  local path="$1"; shift
  local expected actual
  expected="$(printf '%s\n' "$@" | LC_ALL=C sort)"
  actual="$(sudo find "$path" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort)"
  [[ "$actual" == "$expected" ]] || die "unknown_managed_object"
}

verify_no_runner_lifecycle_state() {
  local name
  for name in active-instances create-state remove-state runner-attestations unit-attestations; do
    if sudo test -e "$STATE_ROOT/$name" || sudo test -L "$STATE_ROOT/$name"; then
      die "runner_lifecycle_state_present_during_install"
    fi
  done
}

WEB_BIND_ADDRESS=""
ATTEMPT_ID=""
CURRENT_STAGE="PREFLIGHT"
ACTIVE_STAGE="PREFLIGHT"

if sudo test -e "$INSTALL_STATE" || sudo test -L "$INSTALL_STATE"; then
  read_install_state
  [[ "${INSTALL_FIELDS[0]}" == 1 ]] || die "invalid_install_state_schema"
  [[ "${INSTALL_FIELDS[1]}" == "$RELEASE" ]] || die "partial_release_mismatch"
  [[ "${INSTALL_FIELDS[2]}" == "$PAYLOAD_SHA256" ]] || die "partial_payload_mismatch"
  ATTEMPT_ID="${INSTALL_FIELDS[3]}"
  [[ "${INSTALL_FIELDS[4]}" == "$RUNNER_USER" ]] || die "partial_runner_user_mismatch"
  [[ "${INSTALL_FIELDS[5]}" == "$RUNNER_UID" ]] || die "partial_runner_uid_mismatch"
  [[ "${INSTALL_FIELDS[6]}" == "$RUNNER_GID" ]] || die "partial_runner_gid_mismatch"
  [[ "${INSTALL_FIELDS[7]}" == "$RUNNER_HOME" ]] || die "partial_runner_home_mismatch"
  WEB_BIND_ADDRESS="${INSTALL_FIELDS[8]}"
  CURRENT_STAGE="${INSTALL_FIELDS[9]}"
  [[ "$CURRENT_STAGE" != INSTALL_COMPLETE ]] || die "installation_already_complete"
  check_entries_subset "$STATE_ROOT" install-state.json web-auth-recovery.json
  if ! stage_at_least EXECUTOR_INSTALLED && ( sudo test -e "$AUTH_RECOVERY" || sudo test -L "$AUTH_RECOVERY" ); then
    die "unexpected_admin_provenance_stage"
  fi
  verify_no_runner_lifecycle_state
else
  managed_object_exists_without_state && die "untracked_partial_installation"
  WEB_BIND_ADDRESS="$(grt_select_lan_address)" || die "lan_address_selection_failed"
  ATTEMPT_ID="$(cat /proc/sys/kernel/random/uuid)"
  sudo install -d -o root -g root -m 0700 "$STATE_ROOT"
  write_state ATTEMPT_STARTED
  fault_after ATTEMPT_STARTED
fi

# Persisted bind address is authoritative; never rediscover on retry.
ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$WEB_BIND_ADDRESS" ||
  die "configured_lan_address_not_assigned"

ACTIVE_STAGE="RUNTIME_INSTALLED"
if stage_at_least RUNTIME_INSTALLED; then
  for cmd in python3 jq rsync curl; do need "$cmd"; done
  verify_directory "$INSTALL_ROOT" root root 755
  verify_directory "$CLI_ROOT" root root 755
  verify_directory "$WEB_ROOT" root root 755
  cli_expected=(register-runner.sh remove-runner.sh status-runners.sh cli-operational-logger.py web-runner-cleanup.py web-service-record.py)
  verify_exact_entries "$CLI_ROOT" "${cli_expected[@]}"
  web_expected=()
  for src in "$ROOT"/web/*.py; do web_expected+=("${src##*/}"); done
  verify_exact_entries "$WEB_ROOT" "${web_expected[@]}"
  for f in "${cli_expected[@]}"; do
    verify_file "$ROOT/scripts/$f" "$CLI_ROOT/$f" 755
  done
  for src in "$ROOT"/web/*.py; do verify_file "$src" "$WEB_ROOT/${src##*/}" 755; done
  if stage_at_least ARCHIVE_PLATFORM_INITIALIZED; then
    verify_exact_entries "$INSTALL_ROOT" archive-common.sh cli hooks web
  else
    check_entries_subset "$INSTALL_ROOT" cli web hooks archive-common.sh
  fi
else
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 jq rsync curl ca-certificates
  checked_directory "$INSTALL_ROOT" root root 755
  checked_directory "$CLI_ROOT" root root 755
  checked_directory "$WEB_ROOT" root root 755
  check_entries_subset "$INSTALL_ROOT" cli web
  check_entries_subset "$CLI_ROOT" register-runner.sh remove-runner.sh status-runners.sh cli-operational-logger.py web-runner-cleanup.py web-service-record.py
  web_partial_expected=()
  for src in "$ROOT"/web/*.py; do web_partial_expected+=("${src##*/}"); done
  check_entries_subset "$WEB_ROOT" "${web_partial_expected[@]}"
  for f in register-runner.sh remove-runner.sh status-runners.sh cli-operational-logger.py web-runner-cleanup.py web-service-record.py; do
    checked_install "$ROOT/scripts/$f" "$CLI_ROOT/$f" root root 755
    fault_inside RUNTIME_COPY_PARTIAL
  done
  for src in "$ROOT"/web/*.py; do
    checked_install "$src" "$WEB_ROOT/${src##*/}" root root 755
  done
  write_state RUNTIME_INSTALLED
  fault_after RUNTIME_INSTALLED
fi

ACTIVE_STAGE="AUTHORITY_STATE_INITIALIZED"
if stage_at_least AUTHORITY_STATE_INITIALIZED; then
  getent passwd "$WEB_USER" >/dev/null || die "web_user_missing"
  getent group "$WEB_USER" >/dev/null || die "web_group_missing"
  IFS=: read -r _ _ web_uid web_gid _ web_home web_shell < <(getent passwd "$WEB_USER")
  [[ "$web_uid" -gt 0 && "$web_gid" -gt 0 && "$web_home" == "/nonexistent" &&
      "$web_shell" == "/usr/sbin/nologin" ]] || die "web_user_identity_mismatch"
  [[ "$(getent group "$WEB_USER" | cut -d: -f3)" == "$web_gid" ]] || die "web_group_identity_mismatch"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$STATE_ROOT")" == "root:root:700:directory" ]] || die "state_root_mismatch"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$LOCK_ROOT")" == "root:root:755:directory" ]] || die "lock_root_mismatch"
  sudo test -f "$LOCK_FILE" && ! sudo test -L "$LOCK_FILE" || die "lock_file_mismatch"
  [[ "$(sudo stat -c '%u:%g:%a' "$LOCK_FILE")" == "0:$RUNNER_GID:660" ]] || die "lock_file_mismatch"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$LOG_ROOT")" == "root:$RUNNER_GROUP:2770:directory" ]] || die "log_root_mismatch"
  verify_exact_entries "$LOCK_ROOT" mutation.lock
  verify_exact_entries "$LOG_ROOT" operational.jsonl operational.lock
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$LOG_ROOT/operational.lock")" == "$RUNNER_USER:$RUNNER_GROUP:600:regular empty file" ||
     "$(sudo stat -c '%U:%G:%a:%F' "$LOG_ROOT/operational.lock")" == "$RUNNER_USER:$RUNNER_GROUP:600:regular file" ]] ||
    die "operational_lock_mismatch"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$LOG_ROOT/operational.jsonl")" == "$RUNNER_USER:$RUNNER_GROUP:600:regular empty file" ||
     "$(sudo stat -c '%U:%G:%a:%F' "$LOG_ROOT/operational.jsonl")" == "$RUNNER_USER:$RUNNER_GROUP:600:regular file" ]] ||
    die "operational_log_mismatch"
else
  if ! getent passwd "$WEB_USER" >/dev/null; then
    sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$WEB_USER"
  fi
  getent group "$WEB_USER" >/dev/null || die "web_group_missing"
  IFS=: read -r _ _ web_uid web_gid _ web_home web_shell < <(getent passwd "$WEB_USER")
  [[ "$web_uid" -gt 0 && "$web_gid" -gt 0 && "$web_home" == "/nonexistent" &&
      "$web_shell" == "/usr/sbin/nologin" ]] || die "invalid_web_user"
  [[ "$(getent group "$WEB_USER" | cut -d: -f3)" == "$web_gid" ]] || die "invalid_web_group"
  checked_directory "$LOCK_ROOT" root root 755
  check_entries_subset "$LOCK_ROOT" mutation.lock
  checked_install /dev/null "$LOCK_FILE" root "$RUNNER_GROUP" 660
  checked_directory "$LOG_ROOT" root "$RUNNER_GROUP" 2770
  check_entries_subset "$LOG_ROOT" operational.lock operational.jsonl
  checked_install /dev/null "$LOG_ROOT/operational.lock" "$RUNNER_USER" "$RUNNER_GROUP" 600
  checked_install /dev/null "$LOG_ROOT/operational.jsonl" "$RUNNER_USER" "$RUNNER_GROUP" 600
  write_state AUTHORITY_STATE_INITIALIZED
  fault_after AUTHORITY_STATE_INITIALIZED
fi

ARCHIVE_ROOT="/srv/github-actions-archive"
make_archive_config() {
  cat <<EOF
# managed-by=github-runner-tools
ARCHIVE_ROOT=$ARCHIVE_ROOT
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
EOF
}

ACTIVE_STAGE="ARCHIVE_PLATFORM_INITIALIZED"
if stage_at_least ARCHIVE_PLATFORM_INITIALIZED; then
  verify_directory "$INSTALL_ROOT/hooks" root root 755
  verify_exact_entries "$INSTALL_ROOT/hooks" archive-job-completed.sh
  verify_file "$ROOT/hooks/archive-job-completed.sh" "$INSTALL_ROOT/hooks/archive-job-completed.sh" 755
  verify_file "$ROOT/scripts/lib/archive-common.sh" "$INSTALL_ROOT/archive-common.sh" 644
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$CONFIG_ROOT/archive.conf")" == "root:root:644:regular file" ]] ||
    die "archive_config_metadata_mismatch"
  archive_cfg="$(mktemp)"; make_archive_config >"$archive_cfg"
  sudo cmp -s "$archive_cfg" "$CONFIG_ROOT/archive.conf" || { rm -f "$archive_cfg"; die "archive_config_mismatch"; }
  rm -f "$archive_cfg"
  [[ "$(stat -c '%U:%G:%a:%F' "$ARCHIVE_ROOT")" == "$RUNNER_USER:$RUNNER_GROUP:750:directory" ]] ||
    die "archive_root_mismatch"
else
  checked_directory "$ARCHIVE_ROOT" "$RUNNER_USER" "$RUNNER_GROUP" 750
  fault_inside ARCHIVE_INIT_PARTIAL
  checked_directory "$INSTALL_ROOT/hooks" root root 755
  check_entries_subset "$INSTALL_ROOT" cli web hooks archive-common.sh
  check_entries_subset "$INSTALL_ROOT/hooks" archive-job-completed.sh
  checked_install "$ROOT/hooks/archive-job-completed.sh" "$INSTALL_ROOT/hooks/archive-job-completed.sh" root root 755
  checked_install "$ROOT/scripts/lib/archive-common.sh" "$INSTALL_ROOT/archive-common.sh" root root 644
  checked_directory "$CONFIG_ROOT" root root 755
  check_entries_subset "$CONFIG_ROOT" archive.conf
  archive_tmp="$(mktemp)"
  make_archive_config >"$archive_tmp"
  checked_install "$archive_tmp" "$CONFIG_ROOT/archive.conf" root root 644
  rm -f "$archive_tmp"
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
Environment=PYTHONDONTWRITEBYTECODE=1
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
Environment=PYTHONDONTWRITEBYTECODE=1
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

ACTIVE_STAGE="EXECUTOR_INSTALLED"
if stage_at_least EXECUTOR_INSTALLED; then
  verify_generated "$EXEC_UNIT" make_executor_unit
else
  tmp="$(mktemp)"; make_executor_unit >"$tmp"
  checked_install "$tmp" "$EXEC_UNIT" root root 644; rm -f "$tmp"
  fault_inside EXECUTOR_UNIT_PARTIAL
  write_state EXECUTOR_INSTALLED
  fault_after EXECUTOR_INSTALLED
fi

make_web_config() {
  cat <<EOF
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
}

auth_recovery() {
  local mode="$1" web_gid
  web_gid="$(getent group "$WEB_USER" | cut -d: -f3)"
  sudo /usr/bin/python3 "$ROOT/scripts/install-auth-recovery.py" "$mode" "$STATE_ROOT" "$WEB_AUTH" "$ATTEMPT_ID" "$RELEASE" "$PAYLOAD_SHA256" 0 "$web_gid" ||
    die "unproven_partial_administrator_secret"
}
ACTIVE_STAGE="WEB_INSTALLED"
if stage_at_least WEB_INSTALLED; then
  verify_exact_entries "$STATE_ROOT" install-state.json web-auth-recovery.json
  auth_recovery verify
  verify_exact_entries "$CONFIG_ROOT" archive.conf web-auth.conf web.conf
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$WEB_CONFIG")" == "root:$WEB_USER:640:regular file" ]] || die "web_config_mismatch"
  cfg_verify="$(mktemp)"; make_web_config >"$cfg_verify"
  sudo cmp -s "$cfg_verify" "$WEB_CONFIG" || { rm -f "$cfg_verify"; die "web_config_content_mismatch"; }
  rm -f "$cfg_verify"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$WEB_AUTH")" == "root:$WEB_USER:640:regular file" ]] || die "web_auth_mismatch"
  sudo grep -Eq '^PASSWORD_HASH=scrypt\$' "$WEB_AUTH" || die "web_auth_invalid"
  verify_generated "$WEB_UNIT" make_web_unit
else
  checked_directory "$CONFIG_ROOT" root root 755
  check_entries_subset "$CONFIG_ROOT" archive.conf web.conf web-auth.conf
  cfg="$(mktemp)"
  make_web_config >"$cfg"
  checked_install "$cfg" "$WEB_CONFIG" root "$WEB_USER" 640; rm -f "$cfg"
  fault_inside WEB_CONFIG_PARTIAL
  if sudo test -e "$AUTH_RECOVERY" || sudo test -L "$AUTH_RECOVERY"; then
    # The root-owned attempt-bound journal must match the current release.
    # A published credential must match it byte-for-byte.
    auth_recovery publish
  else
    if sudo test -e "$WEB_AUTH" || sudo test -L "$WEB_AUTH"; then
      die "unproven_partial_administrator_secret"
    fi
  [[ -r /dev/tty && -w /dev/tty ]] || die "administrator_password_requires_tty"
  printf 'Web administrator password: ' >/dev/tty
  IFS= read -r -s password </dev/tty; printf '\n' >/dev/tty
  printf 'Confirm Web administrator password: ' >/dev/tty
  IFS= read -r -s confirm </dev/tty; printf '\n' >/dev/tty
  [[ -n "$password" && "$password" == "$confirm" ]] || { password= confirm=; die "administrator_password_mismatch"; }
  auth_tmp="$(mktemp)"
  # Password travels only through a private pipe to stdin: never argv or env.
  printf '%s' "$password" | PYTHONPATH="$ROOT/web" /usr/bin/python3 -c '
import sys
from grt_web_common import password_hash
secret = sys.stdin.buffer.read(4097)
if not secret or len(secret) > 4096 or b"\\x00" in secret:
    sys.exit(1)
print("PASSWORD_HASH=" + password_hash(secret.decode("utf-8", "strict")))
' >"$auth_tmp" || { password= confirm=; rm -f "$auth_tmp"; die "administrator_password_hash_failed"; }
  password= confirm=
  fault_inside ADMIN_PASSWORD_PARTIAL
    # Journal first, credential second. A crash after either publication
    # can replay only the exact recorded bytes on a matching attempt.
    auth_recovery prepare <"$auth_tmp"
    rm -f "$auth_tmp"
    auth_recovery publish
  fi
  fault_inside ADMIN_HASH_PUBLISHED
  unit_tmp="$(mktemp)"; make_web_unit >"$unit_tmp"
  checked_install "$unit_tmp" "$WEB_UNIT" root root 644; rm -f "$unit_tmp"
  fault_inside WEB_UNIT_PARTIAL
  write_state WEB_INSTALLED
  fault_after WEB_INSTALLED
fi

ACTIVE_STAGE="SERVICES_STARTED"
if stage_at_least SERVICES_STARTED; then
  sudo systemctl is-active --quiet github-runner-tools-executor.service || die "executor_not_active"
  sudo systemctl is-active --quiet github-runner-tools-web.service || die "web_not_active"
  sudo systemctl is-enabled --quiet github-runner-tools-executor.service || die "executor_not_enabled"
  sudo systemctl is-enabled --quiet github-runner-tools-web.service || die "web_not_enabled"
else
  sudo systemctl daemon-reload
  sudo systemctl enable --now github-runner-tools-executor.service
  sudo systemctl enable --now github-runner-tools-web.service
  write_state SERVICES_STARTED
  fault_after SERVICES_STARTED
fi

verify_first_install_smoke() {
  sudo systemctl is-active --quiet github-runner-tools-executor.service || die "executor_smoke_failed"
  sudo systemctl is-active --quiet github-runner-tools-web.service || die "web_smoke_failed"
  sudo test -S /run/github-runner-tools/executor.sock || die "executor_socket_missing"
  web_gid="$(getent group "$WEB_USER" | cut -d: -f3)"
  [[ "$(sudo stat -c '%u:%g:%a:%F' /run/github-runner-tools/executor.sock)" == "0:$web_gid:660:socket" ]] ||
    die "executor_socket_mismatch"
  "$CLI_ROOT/status-runners.sh" --json >/dev/null || die "cli_status_smoke_failed"
  sudo -u "$WEB_USER" /usr/bin/python3 -c "import sys; sys.path.insert(0,'$WEB_ROOT'); from v2_executor_client import web_list; r=web_list(); raise SystemExit(0 if r.get('ok') else 1)" ||
    die "executor_status_smoke_failed"
  tail_line="$(sudo tail -n 1 "$LOG_ROOT/operational.jsonl")"
  [[ "$tail_line" == *'"event":"operation_exit"'* && "$tail_line" == *'"op":"status"'* && "$tail_line" == *'"exit_code":0'* ]] ||
    die "operational_log_smoke_failed"
}

ACTIVE_STAGE="SMOKE_VERIFIED"
verify_first_install_smoke
if ! stage_at_least SMOKE_VERIFIED; then
  write_state SMOKE_VERIFIED
  fault_after SMOKE_VERIFIED
fi

ACTIVE_STAGE="INSTALL_COMPLETE"
write_state INSTALL_COMPLETE

cat <<EOF

github-runner-tools installation complete.
Runner owner : $RUNNER_USER
Web URL      : http://$WEB_BIND_ADDRESS:$WEB_PORT/

LAN HTTP is unencrypted. Use this endpoint only on a trusted home/lab/
management LAN. Do not expose it directly to the public Internet.
EOF
