#!/usr/bin/env bash
set -Eeuo pipefail

# CLI-native operational events; no Web/Executor business log parsing.
GRT_LOGGER="/usr/local/lib/github-runner-tools/cli/cli-operational-logger.py"
GRT_LOG_RUN_ID="$(/usr/bin/python3 -c 'import secrets; print(secrets.token_hex(16))')"
GRT_LOG_SEQ=0
GRT_LOG_READY=0
GRT_LOG_SIDE_EFFECT=0
GRT_LOG_WARNED=0
GRT_LOG_REPO="none"
GRT_LOG_RUNNER="none"
GRT_LOG_STAGE="none"
GRT_LOG_EVENT="preflight"
GRT_LOG_LAST_REASON="none"
grt_log() {
  local event="$1" result="$2" reason="${3:-none}" rc="${4:--1}"
  # An isolated CI source-tree CLI uses the same logger, with a test-only
  # directory. Installed CLI never accepts caller-controlled log locations.
  local -a grt_log_args=()
  if [[ "${GITHUB_ACTIONS:-}" == "true" && "${BASH_SOURCE[0]}" != /usr/local/lib/github-runner-tools/cli/* ]]; then
    GRT_LOGGER="${BASH_SOURCE[0]%/*}/cli-operational-logger.py"
    grt_log_args=(--test-root "${RUNNER_TEMP:-/tmp}/grt-native-logging-$(id -u)")
  fi
  [[ "$result" == "begin" && "$event" != "operation" ]] && GRT_LOG_EVENT="$event"
  GRT_LOG_SEQ=$((GRT_LOG_SEQ + 1))
  if /usr/bin/python3 "$GRT_LOGGER" "${grt_log_args[@]}" --run-id "$GRT_LOG_RUN_ID" --seq "$GRT_LOG_SEQ" --op remove \
       --event "$event" --stage "$GRT_LOG_STAGE" --result "$result" --reason "$reason" \
       --exit-code "$rc" --repository "$GRT_LOG_REPO" --runner "$GRT_LOG_RUNNER"; then
    return 0
  fi
  if [[ "$GRT_LOG_SIDE_EFFECT" == "0" && "remove" != "status" ]]; then
    printf 'ERROR: CLI logging unavailable before side effects.\n' >&2
    exit 1
  fi
  if [[ "$GRT_LOG_WARNED" == "0" ]]; then
    printf 'WARNING: CLI operational logging unavailable; lifecycle authority remains decisive.\n' >&2
    GRT_LOG_WARNED=1
  fi
  return 0
}
grt_log_finish() {
  local rc="$1" reason="none"
  if [[ "$GRT_LOG_READY" == "1" ]]; then
    if [[ "$rc" != 0 ]]; then
      reason="$GRT_LOG_LAST_REASON"
      [[ "$reason" != "none" ]] || reason="$(grt_reason_for_event)"
    fi
    grt_log operation_exit "$([[ "$rc" == 0 ]] && echo ok || echo failed)" "$reason" "$rc" || :
  fi
}

MAX_LOCAL_ID_LENGTH=64
HASH_LENGTH=8
TOKEN=""
MUTATION_LOCK_DIR="/run/lock/github-runner-tools"
MUTATION_LOCK_FILE="$MUTATION_LOCK_DIR/mutation.lock"
WEB_MODE=0
TOKEN_FD=""
PRIVILEGED_FD=""
CLI_REMOVE_PROOF=""
REMOVE_STAGE="preflight_failed"
REMOVE_EXIT="unknown"

# The transport is UTF-8 JSON text followed by one LF. Validate its exact
# bytes before Bash can discard NUL, normalize newlines, or alter encoding.
# Never print invalid bytes. This helper receives the FD number, not a token.

LOCK_ALREADY_HELD=0

usage() {
  cat <<'USAGE'
Usage:
  remove-runner.sh [--base-dir PATH] OWNER/REPO
  remove-runner.sh [--base-dir PATH] --recover-local OWNER/REPO
  remove-runner.sh [--base-dir PATH] --reconcile-incomplete-create OWNER/REPO

Environment variable:
  RUNNER_BASE_DIR
USAGE
}

grt_reason_for_event() {
  # Fixed enum only; never persist any argument or subprocess output.
  case "$GRT_LOG_EVENT" in
    archive_check) echo archive_unavailable ;;
    release_lookup) echo release_metadata_failed ;;
    runner_download) echo download_failed ;;
    digest_check|runner_extract) echo integrity_failed ;;
    dependency_check) echo dependency_failed ;;
    lifecycle_checkpoint) echo checkpoint_failed ;;
    registration_permissions) echo permissions_failed ;;
    registration_attestation) echo attestation_failed ;;
    service_install) echo service_install_failed ;;
    service_start) echo service_start_failed ;;
    service_health) echo service_health_failed ;;
    service_stop) echo service_stop_failed ;;
    service_uninstall) echo service_uninstall_failed ;;
    local_cleanup) echo local_cleanup_failed ;;
    inventory) echo inventory_failed ;;
    remote_registration|remote_removal) echo remote_result_uncertain ;;
    *) echo unexpected_cli_failure ;;
  esac
}
die() {
  local safe_reason
  safe_reason="$GRT_LOG_LAST_REASON"
  [[ "$safe_reason" != "none" ]] || safe_reason="$(grt_reason_for_event)"
  if [[ "$GRT_LOG_READY" == 1 ]]; then
    # A remote outcome may be unknown despite CLI exit 1. Never imply retry.
    if [[ "$GRT_LOG_EVENT" == "remote_registration" || "$GRT_LOG_EVENT" == "remote_removal" ]]; then
      grt_log "$GRT_LOG_EVENT" unknown remote_result_uncertain || :
    else
    case "$GRT_LOG_STAGE" in
      REGISTRATION_OUTCOME_UNKNOWN|REMOTE_REMOVE_OUTCOME_UNKNOWN)
        grt_log "$GRT_LOG_EVENT" unknown remote_result_uncertain || : ;;
      *) GRT_LOG_LAST_REASON="$safe_reason"; grt_log "$GRT_LOG_EVENT" failed "$safe_reason" || : ;;
    esac
    fi
  fi
  echo "ERROR: $*" >&2
  exit 1
}
require_command() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }
cleanup() { unset TOKEN || true; }
trap 'rc=$?; grt_log_finish "$rc"; cleanup' EXIT

sanitize_component() {
  local value="$1" out
  out="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^-+//; s/-+$//')"
  [[ -n "$out" ]] || return 1
  printf '%s' "$out"
}

make_local_id() {
  local owner="$1" repo="$2" safe_owner safe_repo direct normalized hash
  local available owner_len repo_len base_each
  safe_owner="$(sanitize_component "$owner")" || return 1
  safe_repo="$(sanitize_component "$repo")" || return 1
  direct="${safe_owner}--${safe_repo}"
  if (( ${#direct} <= MAX_LOCAL_ID_LENGTH )); then printf '%s' "$direct"; return 0; fi
  normalized="$(printf '%s/%s' "$owner" "$repo" | tr '[:upper:]' '[:lower:]')"
  hash="$(printf '%s' "$normalized" | sha256sum | awk '{print $1}' | cut -c1-${HASH_LENGTH})"
  available=$((MAX_LOCAL_ID_LENGTH - 2 - 2 - HASH_LENGTH))
  base_each=$((available / 2))
  owner_len=${#safe_owner}; repo_len=${#safe_repo}
  if (( owner_len < base_each )); then
    repo_len=$((available-owner_len)); (( repo_len > ${#safe_repo} )) && repo_len=${#safe_repo}; owner_len=$((available-repo_len))
  elif (( repo_len < base_each )); then
    owner_len=$((available-repo_len)); (( owner_len > ${#safe_owner} )) && owner_len=${#safe_owner}; repo_len=$((available-owner_len))
  else
    owner_len=$base_each; repo_len=$((available-owner_len))
  fi
  (( owner_len >= 1 && repo_len >= 1 )) || return 1
  printf '%s--%s--%s' "${safe_owner:0:owner_len}" "${safe_repo:0:repo_len}" "$hash"
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

normalize_repo_url() {
  local value="$1"
  value="${value%/}"
  value="${value%.git}"
  printf '%s' "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
}

metadata_repo_url() {
  local file="$1"
  jq -r '.gitHubUrl // empty' "$file" 2>/dev/null || true
}

path_exists_any() {
  local path="$1"
  [[ -e "$path" || -L "$path" ]]
}

normalize_service_repo_scope() {
  local owner="$1" repo="$2"
  printf '%s-%s' "$owner" "$repo" | sed -E 's/[^0-9A-Za-z._-]/-/g'
}

read_service_name_strict() {
  local file="$1" line
  local -a lines=()

  [[ -f "$file" && ! -L "$file" && -r "$file" ]] || return 1

  mapfile -t lines < "$file" || return 1
  [[ "${#lines[@]}" -eq 1 ]] || return 1

  line="${lines[0]}"
  [[ -n "$line" ]] || return 1
  printf '%s' "$line"
}

service_name_matches_repo_scope() {
  local service_name="$1" owner="$2" repo="$3"
  local scope lower_service lower_prefix suffix runner_segment prefix_len suffix_len

  scope="$(normalize_service_repo_scope "$owner" "$repo")" || return 1
  lower_service="$(printf '%s' "$service_name" | tr '[:upper:]' '[:lower:]')"
  lower_prefix="actions.runner.$(printf '%s' "$scope" | tr '[:upper:]' '[:lower:]')."
  suffix=".service"
  prefix_len=${#lower_prefix}
  suffix_len=${#suffix}

  (( ${#lower_service} > prefix_len + suffix_len )) || return 1
  [[ "${lower_service:0:prefix_len}" == "$lower_prefix" ]] || return 1
  [[ "${lower_service: -suffix_len}" == "$suffix" ]] || return 1

  runner_segment="${lower_service:prefix_len:${#lower_service}-prefix_len-suffix_len}"
  [[ -n "$runner_segment" ]] || return 1
}

validate_recovery_identity() {
  local runner_dir="$1" expected_dir="$2" owner="$3" repo="$4"
  local service_name

  [[ "$runner_dir" == "$expected_dir" ]] ||
    die "Recovery identity cannot be verified: runner directory does not match the expected owner+repository path."

  if path_exists_any "$runner_dir/.runner"; then
    die "Recovery requires .runner to be completely absent. Use normal removal for a configured runner; abnormal .runner residue requires manual review."
  fi

  service_name="$(read_service_name_strict "$runner_dir/.service")" ||
    die "Recovery identity cannot be verified: .service must be one readable regular file containing exactly one non-empty service name."

  service_name_matches_repo_scope "$service_name" "$owner" "$repo" ||
    die "Recovery identity cannot be verified from the complete repository scope in .service. Truncated or mismatched service names require manual cleanup."

  [[ -x "$runner_dir/svc.sh" ]] ||
    die "Runner service-management script is missing or not executable: $runner_dir/svc.sh"

  printf '%s' "$service_name"
}

web_priv_request() {
  local payload="$1" response
  [[ "$WEB_MODE" == "1" && "$PRIVILEGED_FD" =~ ^[0-9]+$ ]] || die "Invalid Web privileged context."
  printf '%s\n' "$payload" >&"$PRIVILEGED_FD" || die "Privileged Executor channel write failed."
  IFS= read -r response <&"$PRIVILEGED_FD" || die "Privileged Executor channel closed."
  WEB_PRIV_RESPONSE="$response"
  jq -e '.ok == true' <<<"$response" >/dev/null 2>&1
}

cli_remove_authority() {
  local stage="$1" authority="/usr/local/lib/github-runner-tools/web/cli_remove_authority.py"
  [[ "$WEB_MODE" != "1" ]] || die "Interactive Remove root authority forbidden in Web context."
  [[ -f "$authority" && ! -L "$authority" ]] ||
    die "Trusted interactive Remove authority unavailable."
  [[ "$(stat -c '%U:%G:%a' "$authority")" == "root:root:755" ]] ||
    die "Interactive Remove authority ownership or mode invalid."
  if [[ "$stage" == "RECONCILE_CONFIRM_REMOTE_REMOVED" ]]; then
    local output=""
    if output="$(sudo /usr/bin/python3 "$authority" "$REPO" "$RUNNER_NAME" "$RUNNER_DIR" "$stage")"; then
      CLI_REMOVE_PROOF="$output"
      return 0
    fi
  elif sudo /usr/bin/python3 "$authority" "$REPO" "$RUNNER_NAME" "$RUNNER_DIR" "$stage"; then
    return 0
  fi
  die "Durable Remove lifecycle checkpoint failed; reconcile before retry."
}

remove_lifecycle_stage() {
  local stage="$1"
  GRT_LOG_STAGE="$stage"
  grt_log lifecycle_checkpoint begin
  [[ "$stage" == "BEGIN" ]] && GRT_LOG_SIDE_EFFECT=1
  if [[ "$WEB_MODE" == "1" ]]; then
    web_remove_state "$stage" || return $?
  else
    cli_remove_authority "$stage" || return $?
  fi
  grt_log lifecycle_checkpoint ok
}

web_remove_state() {
  local stage="$1" payload
  [[ "$WEB_MODE" == "1" ]] || return 1
  payload="$(jq -nc --arg repository "$REPO" --arg runner_dir "$RUNNER_DIR" \
    --arg runner_name "$RUNNER_NAME" --arg stage "$stage" \
    '{op:"remove_state",repository:$repository,runner_dir:$runner_dir,runner_name:$runner_name,stage:$stage}')" || return 1
  web_priv_request "$payload"
}

web_context_check() {
  [[ "$PRIVILEGED_FD" =~ ^[0-9]+$ ]] || die "Invalid Web privileged context."
  python3 - "$PRIVILEGED_FD" <<'PY' || die "Web lifecycle privileged channel is not owned by the root Executor."
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
    [[ "$LOCK_ALREADY_HELD" == "1" ]] || die "Web lifecycle requires Executor-held mutation lock."
    web_context_check
    return 0
  fi
  ensure_mutation_lock
  exec {MUTATION_LOCK_FD}<>"$MUTATION_LOCK_FILE"
  flock -n "$MUTATION_LOCK_FD" || die "Another runner lifecycle operation is already in progress."
}

runner_name_from_service() {
  local service_name="$1" owner="$2" repo="$3" scope lower prefix suffix rest
  scope="$(normalize_service_repo_scope "$owner" "$repo" | tr '[:upper:]' '[:lower:]')"
  lower="$(printf '%s' "$service_name" | tr '[:upper:]' '[:lower:]')"
  prefix="actions.runner.$scope."
  suffix=".service"
  [[ "$lower" == "$prefix"*"$suffix" ]] || return 1
  rest="${service_name:${#prefix}:${#service_name}-${#prefix}-${#suffix}}"
  [[ -n "$rest" ]] || return 1
  printf '%s' "$rest"
}

web_service_state() {
  local repo="$1" runner_dir="$2" runner_name="$3" service="$4" req
  req="$(jq -nc --arg repository "$repo" --arg runner_dir "$runner_dir" --arg runner_name "$runner_name" --arg service "$service" \
    '{op:"service_state",repository:$repository,runner_dir:$runner_dir,runner_name:$runner_name,service:$service}')"
  web_priv_request "$req" || return 1
  jq -er '.state' <<<"$WEB_PRIV_RESPONSE"
}

web_service_operation() {
  local op="$1" repo="$2" runner_dir="$3" runner_name="$4" service="$5" req
  req="$(jq -nc --arg op "$op" --arg repository "$repo" --arg runner_dir "$runner_dir" --arg runner_name "$runner_name" --arg service "$service" \
    '{op:$op,repository:$repository,runner_dir:$runner_dir,runner_name:$runner_name,service:$service}')"
  web_priv_request "$req"
}

read_tty_line() {
  local prompt="$1" secret="${2:-0}" value
  [[ -r /dev/tty && -w /dev/tty ]] || die "Interactive input requires a TTY."
  if [[ "$secret" == "1" ]]; then
    IFS= read -r -s -p "$prompt" value < /dev/tty
    printf '\n' > /dev/tty
  else
    IFS= read -r -p "$prompt" value < /dev/tty
  fi
  printf '%s' "$value"
}

runner_service_state() {
  local service_name load_state

  if [[ ! -f .service ]]; then
    printf '%s' "unknown"
    return 0
  fi

  service_name="$(tr -d '\r\n' < .service)"
  if [[ -z "$service_name" ]]; then
    printf '%s' "unknown"
    return 0
  fi

  load_state="$(systemctl show "$service_name" -p LoadState --value 2>/dev/null || true)"
  case "$load_state" in
    not-found) printf '%s' "absent" ;;
    "")        printf '%s' "unknown" ;;
    *)         printf '%s' "present" ;;
  esac
}

uninstall_service_safely() {
  local state

  state="$(runner_service_state)"
  case "$state" in
    absent)
      echo "==> Systemd service is already absent; continuing."
      return 0
      ;;
    unknown)
      die "Cannot determine systemd service state because .service is missing, empty, or unreadable by systemctl. Local runner directory will not be deleted."
      ;;
    present) ;;
    *)
      die "Unexpected systemd service state: $state"
      ;;
  esac

  if sudo ./svc.sh uninstall; then
    return 0
  fi

  state="$(runner_service_state)"
  case "$state" in
    absent)
      echo "WARNING: svc.sh uninstall returned non-zero, but the systemd service is now absent; continuing." >&2
      return 0
      ;;
    unknown)
      die "svc.sh uninstall failed and the resulting systemd service state is unknown. Local runner directory will not be deleted."
      ;;
    present)
      die "Systemd service uninstall failed and the service still appears to exist. Local runner directory will not be deleted."
      ;;
    *)
      die "Unexpected systemd service state after uninstall attempt: $state"
      ;;
  esac
}

remove_local_runner_dir() {
  # One post-confirmation cleanup path, shared by both modes. The Web
  # transport receives inode/device proof from the existing root authority;
  # interactive CLI uses its user-owned scoped directory deletion.
  [[ -n "$RUNNER_DIR" && "$RUNNER_DIR" != "/" ]] ||
    die "Cannot delete an empty or root Runner directory."
  case "$RUNNER_DIR" in
    "$RUNNER_BASE_DIR"/actions-runner-*) ;;
    *) die "Unexpected local runner deletion scope." ;;
  esac
  if [[ "$WEB_MODE" == "1" ]]; then
    python3 "${BASH_SOURCE[0]%/*}/web-runner-cleanup.py" \
      "$RUNNER_BASE_DIR" "$RUNNER_DIR" "$REPO" "$REMOVE_PROOF_DEVICE" "$REMOVE_PROOF_INODE" ||
      die "Runner local directory cleanup failed; remote unregister was already completed."
  else
    rm -rf -- "$RUNNER_DIR"
    [[ ! -e "$RUNNER_DIR" && ! -L "$RUNNER_DIR" ]] ||
      die "Runner directory still exists after local cleanup."
  fi
}

main() {
if [[ ${EUID} -eq 0 ]]; then die "Do not run this script as root. Run it as the runner owner."; fi
for cmd in jq id awk tr sed sha256sum cut find systemctl flock stat python3; do require_command "$cmd"; done
if [[ "$WEB_MODE" != "1" ]]; then require_command sudo; fi

CLI_BASE_DIR=""; RECOVER_LOCAL=0; RECONCILE_INCOMPLETE=0; POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir) [[ $# -ge 2 ]] || die "--base-dir requires a value"; CLI_BASE_DIR="$2"; shift 2 ;;
    --recover-local) RECOVER_LOCAL=1; shift ;;
    --reconcile-incomplete-create) RECONCILE_INCOMPLETE=1; shift ;;
    --executor-mode) WEB_MODE=1; shift ;;
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
  [[ "${GRT_EXECUTOR_CONTEXT:-0}" == "1" ]] ||
    die "Internal Executor mode requires trusted context."
  [[ "$PRIVILEGED_FD" =~ ^[0-9]+$ && "$LOCK_ALREADY_HELD" == "1" ]] ||
    die "Incomplete internal Executor lifecycle context."
  if [[ "$RECOVER_LOCAL" != "1" && "$RECONCILE_INCOMPLETE" != "1" ]]; then
    [[ "$TOKEN_FD" =~ ^[0-9]+$ ]] || die "Normal noninteractive removal requires token FD."
  fi
  web_context_check
elif [[ -n "$TOKEN_FD" || -n "$PRIVILEGED_FD" || "$LOCK_ALREADY_HELD" == "1" ]]; then
  die "Internal Executor options are not available in normal CLI mode."
fi

REPO="$1"
GRT_LOG_READY=1
if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ || ${#REPO} -gt 201 ]]; then
  GRT_LOG_REPO="none"
  grt_log operation failed invalid_input
  echo "ERROR: Repository must be in OWNER/REPO form." >&2
  exit 1
fi
GRT_LOG_REPO="$REPO"
grt_log operation begin
OWNER="${REPO%%/*}"; REPO_NAME="${REPO##*/}"
SAFE_REPO="$(sanitize_component "$REPO_NAME")" || die "Could not derive safe repository name."
LOCAL_ID="$(make_local_id "$OWNER" "$REPO_NAME")" || die "Could not derive local identity."
RUNNER_USER="$(id -un)"; USER_HOME="$(resolve_home "$RUNNER_USER")" || die "Could not resolve home for $RUNNER_USER"
RUNNER_BASE_DIR="${CLI_BASE_DIR:-${RUNNER_BASE_DIR:-$USER_HOME}}"
[[ -d "$RUNNER_BASE_DIR" && -x "$RUNNER_BASE_DIR" && -r "$RUNNER_BASE_DIR" && -w "$RUNNER_BASE_DIR" ]] || die "RUNNER_BASE_DIR is not accessible and writable: $RUNNER_BASE_DIR"
RUNNER_BASE_DIR="$(canonicalize_existing_dir "$RUNNER_BASE_DIR")" || die "Could not canonicalize RUNNER_BASE_DIR: $RUNNER_BASE_DIR"

acquire_mutation_lock

NEW_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"
LEGACY_DIR="$RUNNER_BASE_DIR/actions-runner-$SAFE_REPO"
RUNNER_DIR=""
MODE=""

if [[ "$RECOVER_LOCAL" == "1" ]]; then
  [[ -d "$NEW_DIR" ]] || {
    if [[ -d "$LEGACY_DIR" ]]; then
      die "Recovery target uses the legacy repo-only directory and cannot be identified safely without .runner metadata: $LEGACY_DIR"
    fi
    die "Recovery target not found for $REPO: $NEW_DIR"
  }

  RUNNER_DIR="$(canonicalize_existing_dir "$NEW_DIR")" ||
    die "Could not canonicalize recovery target: $NEW_DIR"
  EXPECTED_NEW_DIR="$NEW_DIR"

  SERVICE_NAME="$(validate_recovery_identity "$RUNNER_DIR" "$EXPECTED_NEW_DIR" "$OWNER" "$REPO_NAME")" ||
    die "Recovery identity validation failed. No local service mutation or directory deletion was performed."

  cd "$RUNNER_DIR"
  if [[ "$WEB_MODE" == "1" ]]; then
    RUNNER_NAME="$(runner_name_from_service "$SERVICE_NAME" "$OWNER" "$REPO_NAME")" ||
      die "Could not establish runner name from verified recovery service identity."
    SERVICE_STATE="$(web_service_state "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME")" ||
      die "Cannot determine privileged systemd service state."
  else
    SERVICE_STATE="$(runner_service_state)"
  fi
  case "$SERVICE_STATE" in
    present|active|inactive|absent) ;;
    unknown)
      die "Cannot determine systemd service state. Local runner directory will not be deleted."
      ;;
    *)
      die "Unexpected systemd service state: $SERVICE_STATE"
      ;;
  esac

  cat <<INFO
Repository : $REPO
Runner dir : $RUNNER_DIR
Mode       : local recovery
Service    : $SERVICE_NAME
State      : $SERVICE_STATE

This mode performs LOCAL cleanup only.
It will not request a GitHub removal token and will not call config.sh remove.
Local artifact archives are not deleted.
INFO

  if [[ "$WEB_MODE" != "1" ]]; then
    CONFIRM="$(read_tty_line "Type RECOVER-REMOVE to uninstall local residue and delete this runner directory: ")"
    [[ "$CONFIRM" == "RECOVER-REMOVE" ]] || die "Cancelled."
  fi

  # Local-only recovery changes systemd/files without remote registration.
  # A logger failure must not abort after this existing mutation boundary.
  GRT_LOG_SIDE_EFFECT=1
  if [[ "$SERVICE_STATE" != "absent" ]]; then
    grt_log service_stop begin
    echo "==> Stopping service..."
    if [[ "$WEB_MODE" == "1" ]]; then
      web_service_operation service_stop "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME" ||
        die "Privileged service stop failed."
      web_service_operation service_uninstall "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME" ||
        die "Privileged service uninstall failed."
    else
      if ! sudo ./svc.sh stop; then
        echo "WARNING: Service stop failed; continuing to service uninstall so the final systemd state can be checked." >&2
      fi
      echo "==> Uninstalling service..."
      uninstall_service_safely
    fi
  else
    echo "==> Systemd service is already absent; continuing."
  fi

  if [[ "$WEB_MODE" == "1" ]]; then
    FINAL_SERVICE_STATE="$(web_service_state "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME")" ||
      die "Could not verify final privileged service state."
  else
    FINAL_SERVICE_STATE="$(runner_service_state)"
  fi
  [[ "$FINAL_SERVICE_STATE" == "absent" ]] ||
    die "Systemd service is not confirmed absent after recovery cleanup. Local runner directory will not be deleted."

  cd "$RUNNER_BASE_DIR"
  [[ -n "$RUNNER_DIR" ]] || die "Safety check failed: empty recovery runner path."
  [[ "$RUNNER_DIR" == "$EXPECTED_NEW_DIR" ]] ||
    die "Safety check failed: recovery target changed unexpectedly."
  case "$RUNNER_DIR" in
    "$RUNNER_BASE_DIR"/actions-runner-*) ;;
    *) die "Safety check failed; refusing to delete unexpected path: $RUNNER_DIR" ;;
  esac

  echo "==> Deleting local runner directory..."
  rm -rf -- "$RUNNER_DIR"

  echo
  cat <<DONE
Local runner residue removed successfully.
Repository: https://github.com/$REPO
Deleted:    $RUNNER_DIR
Artifacts:  preserved
DONE
  exit 0
fi

if [[ -d "$NEW_DIR" ]]; then
  RUNNER_DIR="$NEW_DIR"; MODE="new"
fi
if [[ -d "$LEGACY_DIR" ]]; then
  if [[ -f "$LEGACY_DIR/.runner" ]]; then
    META_URL="$(metadata_repo_url "$LEGACY_DIR/.runner")"
  else
    META_URL=""
  fi
  EXPECTED_URL="https://github.com/$REPO"

  if [[ -n "$META_URL" && "$(normalize_repo_url "$META_URL")" == "$(normalize_repo_url "$EXPECTED_URL")" ]]; then
    if [[ -n "$RUNNER_DIR" && "$RUNNER_DIR" != "$LEGACY_DIR" ]]; then
      die "Both new and verified legacy runner directories exist. Refusing to guess which one to remove."
    fi
    RUNNER_DIR="$LEGACY_DIR"; MODE="legacy"
  elif [[ -z "$RUNNER_DIR" ]]; then
    if [[ -z "$META_URL" ]]; then
      die "Legacy runner candidate is ambiguous because repository metadata cannot be verified: $LEGACY_DIR"
    fi
    die "Legacy runner directory belongs to a different repository: $META_URL"
  fi
fi

[[ -n "$RUNNER_DIR" ]] || die "Runner directory not found for $REPO"
[[ -f "$RUNNER_DIR/.runner" ]] || die "Configured runner metadata not found: $RUNNER_DIR/.runner"

if [[ "$RECONCILE_INCOMPLETE" == "1" ]]; then
  [[ "$WEB_MODE" != "1" ]] || die "Incomplete Create reconciliation is interactive CLI-only."
  [[ "$RECOVER_LOCAL" != "1" ]] || die "Recovery modes cannot be combined."
  [[ -x "$RUNNER_DIR/config.sh" ]] || die "Runner config.sh is missing or not executable."
  RUNNER_NAME="$(jq -er '.agentName' "$RUNNER_DIR/.runner")" ||
    die "Configured Runner name is unavailable."
  GRT_LOG_RUNNER="$RUNNER_NAME"
  cat <<INFO
Repository : $REPO
Runner dir : $RUNNER_DIR
Mode       : incomplete Create reconciliation
Runner     : $RUNNER_NAME

This path is only for an authority-attested REGISTERED_UNIT_INCOMPLETE
Create with no Unit and no .service. It writes durable Remove intent before
calling the official GitHub unregister flow. Any uncertain remote result
preserves all local evidence and stops.
INFO
  TOKEN="$(read_tty_line "Paste fresh GitHub removal token: " 1)"
  [[ -n "$TOKEN" ]] || die "Removal token cannot be empty."
  CONFIRM="$(read_tty_line "Type RECONCILE-REMOVE to unregister this failed Create: ")"
  [[ "$CONFIRM" == "RECONCILE-REMOVE" ]] || die "Cancelled."

  GRT_LOG_STAGE="BEGIN"
  grt_log lifecycle_checkpoint begin
  cli_remove_authority RECONCILE_BEGIN
  GRT_LOG_SIDE_EFFECT=1
  grt_log lifecycle_checkpoint ok

  cd "$RUNNER_DIR"
  GRT_LOG_STAGE="REMOTE_REMOVE_OUTCOME_UNKNOWN"
  grt_log remote_removal begin
  if ./config.sh remove --token "$TOKEN"; then
    unset TOKEN
    grt_log remote_removal ok
  else
    REMOVE_EXIT=$?
    unset TOKEN
    GRT_LOG_LAST_REASON="remote_result_uncertain"
    grt_log remote_removal unknown remote_result_uncertain
    die "Runner registration removal failed or remains uncertain."
  fi

  GRT_LOG_STAGE="CONFIRM_REMOTE_REMOVED"
  grt_log lifecycle_checkpoint begin
  cli_remove_authority RECONCILE_CONFIRM_REMOTE_REMOVED
  RECONCILE_PROOF_DEVICE="$(jq -er '.device | numbers' <<<"$CLI_REMOVE_PROOF")" || die "Reconciliation authority returned invalid target device."
  RECONCILE_PROOF_INODE="$(jq -er '.inode | numbers' <<<"$CLI_REMOVE_PROOF")" || die "Reconciliation authority returned invalid target inode."
  grt_log lifecycle_checkpoint ok

  cd "$RUNNER_BASE_DIR"
  case "$RUNNER_DIR" in
    "$RUNNER_BASE_DIR"/actions-runner-*) ;;
    *) die "Safety check failed; refusing to delete unexpected path: $RUNNER_DIR" ;;
  esac
  GRT_LOG_STAGE="REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING"
  grt_log local_cleanup begin
  python3 "${BASH_SOURCE[0]%/*}/web-runner-cleanup.py" "$RUNNER_BASE_DIR" "$RUNNER_DIR" "$REPO" "$RECONCILE_PROOF_DEVICE" "$RECONCILE_PROOF_INODE" || die "Bounded reconciliation cleanup rejected; remote unregister is confirmed and local evidence is preserved."
  [[ ! -e "$RUNNER_DIR" && ! -L "$RUNNER_DIR" ]] ||
    die "Runner directory still exists after bounded reconciliation cleanup."
  grt_log local_cleanup ok

  GRT_LOG_STAGE="CREATE_ABORTED_RECONCILED"
  grt_log lifecycle_checkpoint begin
  cli_remove_authority RECONCILE_COMPLETE
  grt_log lifecycle_checkpoint ok
  echo "Incomplete Create reconciled and removed successfully."
  exit 0
fi

[[ -x "$RUNNER_DIR/config.sh" && -x "$RUNNER_DIR/svc.sh" ]] || die "Runner management scripts are missing from: $RUNNER_DIR"

cat <<INFO
Repository : $REPO
Runner dir : $RUNNER_DIR
Mode       : $MODE

Before continuing, open:
  https://github.com/$REPO/settings/actions/runners

Select the runner, choose Remove, and copy the temporary removal token.
INFO

RUNNER_NAME="$(jq -er '.agentName' "$RUNNER_DIR/.runner")" ||
  die "Configured Runner name is unavailable."
if [[ "$WEB_MODE" != "1" ]]; then
  TOKEN="$(read_tty_line "Paste GitHub removal token: " 1)"
  [[ -n "$TOKEN" ]] || die "Removal token cannot be empty."
  CONFIRM="$(read_tty_line "Type REMOVE to unregister and delete this runner: ")"
  [[ "$CONFIRM" == "REMOVE" ]] || die "Cancelled."
  remove_lifecycle_stage BEGIN
fi

cd "$RUNNER_DIR"
if [[ "$WEB_MODE" == "1" ]]; then
  RUNNER_NAME="$(jq -er '.agentName' "$RUNNER_DIR/.runner")" || die "Configured runner name is unavailable."
  SERVICE_NAME="actions.runner.${OWNER}-${REPO_NAME}.${RUNNER_NAME}.service"
  [[ "${#SERVICE_NAME}" -le 150 ]] || die "Unsupported service identity."
  REMOVE_STAGE="preflight_failed"
  python3 "${BASH_SOURCE[0]%/*}/web-service-record.py" identity "$RUNNER_BASE_DIR" "$RUNNER_DIR" "$REPO" "$RUNNER_NAME" "$SERVICE_NAME" ||
    die "Configured runner identity cannot be verified."
  if [[ ! -e "$RUNNER_DIR/.service" && ! -L "$RUNNER_DIR/.service" ]]; then
    REMOVE_STAGE="unknown_failed"
    die "Runner service record missing; previous remote outcome requires manual review."
  fi
  if compgen -G "$RUNNER_DIR/.grt-service-reconcile-*" >/dev/null; then
    REMOVE_STAGE="unknown_failed"
    die "Previous service record quarantine requires manual review."
  fi
  REMOVE_STAGE="permission_reconcile_failed"
  python3 "${BASH_SOURCE[0]%/*}/web-service-record.py" registration_check "$RUNNER_BASE_DIR" "$RUNNER_DIR" "$REPO" "$RUNNER_NAME" "$SERVICE_NAME" ||
    die "Runner registration file permissions require trusted administrator review."
  REMOVE_STAGE="service_record_reconcile_failed"
  [[ -f "$RUNNER_DIR/.service" && ! -L "$RUNNER_DIR/.service" ]] ||
    die "Runner service record unsafe; manual inspection required."
  python3 "${BASH_SOURCE[0]%/*}/web-service-record.py" check "$RUNNER_BASE_DIR" "$RUNNER_DIR" "$REPO" "$RUNNER_NAME" "$SERVICE_NAME" ||
    die "Configured service identity is unavailable or incompatible."
  REMOVE_STAGE="preflight_failed"
  remove_lifecycle_stage BEGIN || die "Cannot persist removal attempt identity; no service mutation started."
  REMOVE_STAGE="service_state_failed"
  SERVICE_STATE="$(web_service_state "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME")" ||
    die "Could not validate privileged systemd service state."
  if [[ "$SERVICE_STATE" != "absent" ]]; then
    REMOVE_STAGE="service_stop_failed"
    grt_log service_stop begin
    web_service_operation service_stop "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME" ||
      die "Privileged service stop failed."
    grt_log service_stop ok
    REMOVE_STAGE="service_uninstall_failed"
    grt_log service_uninstall begin
    web_service_operation service_uninstall "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME" ||
      die "Privileged service uninstall failed."
    grt_log service_uninstall ok
  fi
  REMOVE_STAGE="service_uninstall_failed"
  FINAL_SERVICE_STATE="$(web_service_state "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME")" ||
    die "Cannot verify final service state before record reconciliation."
  [[ "$FINAL_SERVICE_STATE" == "absent" ]] ||
    die "Systemd unit still exists; preserving service record."
  REMOVE_STAGE="service_record_reconcile_failed"
  RECORD_PROOF="$(python3 "${BASH_SOURCE[0]%/*}/web-service-record.py" quarantine "$RUNNER_BASE_DIR" "$RUNNER_DIR" "$REPO" "$RUNNER_NAME" "$SERVICE_NAME")" ||
    die "Local service record reconciliation failed."
  REMOVE_STAGE="service_uninstall_failed"
  FINAL_SERVICE_STATE="$(web_service_state "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME")" ||
    die "Cannot verify final service state after record reconciliation."
  [[ "$FINAL_SERVICE_STATE" == "absent" ]] ||
    die "Systemd service state changed after reconciliation."
  REMOVE_STAGE="service_record_reconcile_failed"
  python3 "${BASH_SOURCE[0]%/*}/web-service-record.py" verify "$RUNNER_BASE_DIR" "$RUNNER_DIR" "$REPO" "$RUNNER_NAME" "$SERVICE_NAME" "$RECORD_PROOF" ||
    die "Local service record identity changed after reconciliation."
else
  grt_log service_stop begin
  echo "==> Stopping service..."
  if ! sudo ./svc.sh stop; then
    echo "WARNING: Service stop failed; continuing to service uninstall in case it is already stopped." >&2
  fi
  grt_log service_stop ok
  grt_log service_uninstall begin
  echo "==> Uninstalling service..."
  uninstall_service_safely
  grt_log service_uninstall ok
fi

grt_log remote_removal begin
echo "==> Removing runner registration from GitHub..."
if [[ "$WEB_MODE" == "1" ]]; then
  REMOVE_STAGE="config_remove_failed"
  # The Executor consumes the one-shot FD through the existing PTY adapter.
  # Never expose the token in CLI argv, env or persistent state.
  PTY_ADAPTER="${GRT_PTY_ADAPTER:-/usr/local/lib/github-runner-tools/web/pty_token_adapter.py}"
  [[ -x "$PTY_ADAPTER" ]] || die "Trusted removal token adapter unavailable."
  if (
    # Root privilege FD4 belongs only to the trusted CLI shell. The
    # external GitHub config and PTY adapter must never inherit it.
    exec 4>&-
    python3 "$PTY_ADAPTER" --token-fd "$TOKEN_FD" --mode remove -- ./config.sh remove >/dev/null 2>&1
  ); then
    REMOVE_RC=0
  else
    REMOVE_RC=$?
  fi
  # The adapter owns the single read; close Bash's inherited copy before
  # any subsequent privileged file or service operation, on both outcomes.
  exec {TOKEN_FD}<&-
  if [[ "$REMOVE_RC" == "0" ]]; then
    grt_log remote_removal ok
    unset TOKEN
    REMOVE_STAGE="local_cleanup_failed"
    remove_lifecycle_stage CONFIRM_REMOTE_REMOVED ||
      die "Official unregister succeeded but durable local cleanup authorization failed."
    REMOVE_PROOF_DEVICE="$(jq -er '.device | numbers' <<<"$WEB_PRIV_RESPONSE")" ||
      die "Cleanup authority returned invalid target device."
    REMOVE_PROOF_INODE="$(jq -er '.inode | numbers' <<<"$WEB_PRIV_RESPONSE")" ||
      die "Cleanup authority returned invalid target inode."
  else
    REMOVE_EXIT="$REMOVE_RC"
    # Bash's 128+signal convention cannot distinguish a killed child from
    # a normal program deliberately exiting with the same numeric status.
    # All such ambiguous statuses are conservatively unknown.
    if (( REMOVE_EXIT >= 128 )); then
      REMOVE_STAGE="unknown_failed"
      REMOVE_EXIT="unknown"
    fi
    unset TOKEN
    grt_log remote_removal unknown remote_result_uncertain
    die "Runner registration removal failed or remains uncertain."
  fi
else
  ./config.sh remove --token "$TOKEN"
  unset TOKEN
  grt_log remote_removal ok
  remove_lifecycle_stage CONFIRM_REMOTE_REMOVED
fi

REMOVE_STAGE="local_cleanup_failed"
cd "$RUNNER_BASE_DIR"
case "$RUNNER_DIR" in "$RUNNER_BASE_DIR"/actions-runner-*) ;; *) die "Safety check failed; refusing to delete unexpected path: $RUNNER_DIR" ;; esac

grt_log local_cleanup begin
echo "==> Deleting local runner directory..."
remove_local_runner_dir
grt_log local_cleanup ok
remove_lifecycle_stage COMPLETE ||
  die "Runner directory deletion completed but durable Remove closeout failed."

echo
cat <<DONE
Runner removed successfully.
Repository: https://github.com/$REPO
Deleted:    $RUNNER_DIR
DONE


}

if [[ "${RUNNER_TOOLS_LIB_ONLY:-0}" != "1" ]]; then
  main "$@"
fi
