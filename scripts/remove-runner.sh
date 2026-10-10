#!/usr/bin/env bash
set -Eeuo pipefail

MAX_LOCAL_ID_LENGTH=64
HASH_LENGTH=8
TOKEN=""
MUTATION_LOCK_DIR="/run/lock/github-runner-tools"
MUTATION_LOCK_FILE="$MUTATION_LOCK_DIR/mutation.lock"
WEB_MODE=0
TOKEN_FD=""
PRIVILEGED_FD=""
RESULT_FD=""
REMOVE_STAGE="preflight_failed"
REMOVE_EXIT="unknown"
REMOVE_MARKED=0
remove_result() {
  local rc="$1" value
  [[ "$WEB_MODE" == "1" && -n "$RESULT_FD" && "$REMOVE_MARKED" == "0" && "$rc" -ne 0 ]] || return 0
  REMOVE_MARKED=1
  case "$REMOVE_STAGE" in
    preflight_failed|service_state_failed|service_stop_failed|service_uninstall_failed|service_record_reconcile_failed|permission_reconcile_failed|config_remove_failed|local_cleanup_failed|unknown_failed) ;;
    *) REMOVE_STAGE="unknown_failed" ;;
  esac
  value="$REMOVE_EXIT"
  [[ "$value" =~ ^(0|[1-9][0-9]{0,2})$ ]] || value="unknown"
  if [[ "$value" != "unknown" ]] && (( value > 255 )); then value="unknown"; fi
  printf 'GRT_REMOVE_RESULT_V1 stage=%s exit=%s\n' "$REMOVE_STAGE" "$value" >&"$RESULT_FD" 2>/dev/null || true
}
# Web Remove external tools are invoked through wrappers that close the
# private diagnostic write descriptor in the child before executing the tool.
# Bash builtins and the parent remain able to write the final result.
web_remove_external() {
  if [[ "$WEB_MODE" == "1" && -n "$RESULT_FD" ]]; then
    (exec {RESULT_FD}>&-; command "$@")
  else
    command "$@"
  fi
}
python3() { web_remove_external python3 "$@"; }
cat() { web_remove_external cat "$@"; }
jq() { web_remove_external jq "$@"; }
tr() { web_remove_external tr "$@"; }
sed() { web_remove_external sed "$@"; }
sha256sum() { web_remove_external sha256sum "$@"; }
awk() { web_remove_external awk "$@"; }
cut() { web_remove_external cut "$@"; }
getent() { web_remove_external getent "$@"; }
id() { web_remove_external id "$@"; }
stat() { web_remove_external stat "$@"; }
systemctl() { web_remove_external systemctl "$@"; }
find() { web_remove_external find "$@"; }
flock() { web_remove_external flock "$@"; }
sudo() { web_remove_external sudo "$@"; }
install() { web_remove_external install "$@"; }
touch() { web_remove_external touch "$@"; }
chown() { web_remove_external chown "$@"; }
chmod() { web_remove_external chmod "$@"; }
mkdir() { web_remove_external mkdir "$@"; }
mv() { web_remove_external mv "$@"; }
rm() { web_remove_external rm "$@"; }
sleep() { web_remove_external sleep "$@"; }
grep() { web_remove_external grep "$@"; }
sort() { web_remove_external sort "$@"; }
wc() { web_remove_external wc "$@"; }

# The transport is UTF-8 JSON text followed by one LF. Validate its exact
# bytes before Bash can discard NUL, normalize newlines, or alter encoding.
# Never print invalid bytes. This helper receives the FD number, not a token.
web_token_from_fd() {
  local value status=0
  [[ "$TOKEN_FD" =~ ^[0-9]+$ ]] || return 1
  value="$(python3 - "$TOKEN_FD" <<'PY'
import os
import sys
fd = int(sys.argv[1])
raw = bytearray()
while len(raw) <= 4096:
    chunk = os.read(fd, 4097 - len(raw))
    if not chunk:
        break
    raw.extend(chunk)
    if b"\n" in chunk:
        break
if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
    raise SystemExit(1)
payload = bytes(raw[:-1])
try:
    value = payload.decode("utf-8", "strict")
except UnicodeError:
    raise SystemExit(1)
if (not value or len(value) > 1024 or
        any(char in value for char in ("\x00", "\r", "\n"))):
    raise SystemExit(1)
sys.stdout.buffer.write(payload)
PY
)" || status=$?
  # Close the original inherited descriptor in the parent immediately,
  # including validation failure, before any service-related subprocess.
  exec {TOKEN_FD}<&-
  [[ "$status" == "0" ]] || return 1
  TOKEN="$value"
}

LOCK_ALREADY_HELD=0

usage() {
  cat <<'USAGE'
Usage:
  remove-runner.sh [--base-dir PATH] OWNER/REPO
  remove-runner.sh [--base-dir PATH] --recover-local OWNER/REPO

Environment variable:
  RUNNER_BASE_DIR
USAGE
}

die() { echo "ERROR: $*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }
cleanup() { unset TOKEN || true; }
trap 'rc=$?; remove_result "$rc"; cleanup' EXIT

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
  printf '%s\n' "$payload" >&"$PRIVILEGED_FD" || die "Privileged dispatcher channel write failed."
  IFS= read -r response <&"$PRIVILEGED_FD" || die "Privileged dispatcher channel closed."
  WEB_PRIV_RESPONSE="$response"
  jq -e '.ok == true' <<<"$response" >/dev/null 2>&1
}

web_context_check() {
  [[ "$PRIVILEGED_FD" =~ ^[0-9]+$ ]] || die "Invalid Web privileged context."
  python3 - "$PRIVILEGED_FD" <<'PY' || die "Web lifecycle privileged channel is not owned by the root dispatcher."
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
  web_priv_request '{"op":"context_check"}' || die "Web lifecycle context is not authorized by dispatcher."
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
    [[ "$LOCK_ALREADY_HELD" == "1" ]] || die "Web lifecycle requires dispatcher-held mutation lock."
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

main() {
if [[ ${EUID} -eq 0 ]]; then die "Do not run this script as root. Run it as the runner owner."; fi
for cmd in jq id awk tr sed sha256sum cut find systemctl flock stat python3; do require_command "$cmd"; done
if [[ "$WEB_MODE" != "1" ]]; then require_command sudo; fi

CLI_BASE_DIR=""; RECOVER_LOCAL=0; POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir) [[ $# -ge 2 ]] || die "--base-dir requires a value"; CLI_BASE_DIR="$2"; shift 2 ;;
    --recover-local) RECOVER_LOCAL=1; shift ;;
    --web-worker) WEB_MODE=1; shift ;;
    --token-fd) [[ $# -ge 2 ]] || die "--token-fd requires a value"; TOKEN_FD="$2"; shift 2 ;;
    --result-fd) [[ $# -ge 2 ]] || die "--result-fd requires a value"; RESULT_FD="$2"; shift 2 ;;
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
  [[ "${GRT_WEB_CONTEXT:-0}" == "1" ]] || die "Internal Web mode requires dispatcher context."
  [[ "$PRIVILEGED_FD" =~ ^[0-9]+$ && "$LOCK_ALREADY_HELD" == "1" ]] || die "Incomplete internal Web lifecycle context."
  if [[ "$RECOVER_LOCAL" != "1" ]]; then
    [[ "$TOKEN_FD" =~ ^[0-9]+$ && "$RESULT_FD" =~ ^[0-9]+$ ]] || die "Normal Web removal requires token and result FD."
  fi
  web_context_check
elif [[ -n "$TOKEN_FD" || -n "$RESULT_FD" || -n "$PRIVILEGED_FD" || "$LOCK_ALREADY_HELD" == "1" ]]; then
  die "Internal Web options are not available in normal CLI mode."
fi

REPO="$1"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "Repository must be in OWNER/REPO form."
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

  if [[ "$SERVICE_STATE" != "absent" ]]; then
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
[[ -x "$RUNNER_DIR/config.sh" && -x "$RUNNER_DIR/svc.sh" ]] || die "Runner management scripts are missing from: $RUNNER_DIR"

cat <<INFO
Repository : $REPO
Runner dir : $RUNNER_DIR
Mode       : $MODE

Before continuing, open:
  https://github.com/$REPO/settings/actions/runners

Select the runner, choose Remove, and copy the temporary removal token.
INFO

if [[ "$WEB_MODE" != "1" ]]; then
  TOKEN="$(read_tty_line "Paste GitHub removal token: " 1)"
  [[ -n "$TOKEN" ]] || die "Removal token cannot be empty."
  CONFIRM="$(read_tty_line "Type REMOVE to unregister and delete this runner: ")"
  [[ "$CONFIRM" == "REMOVE" ]] || die "Cancelled."
fi

cd "$RUNNER_DIR"
if [[ "$WEB_MODE" == "1" ]]; then
  web_token_from_fd || die "Invalid Web removal token input."
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
  REMOVE_STAGE="service_state_failed"
  SERVICE_STATE="$(web_service_state "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME")" ||
    die "Could not validate privileged systemd service state."
  if [[ "$SERVICE_STATE" != "absent" ]]; then
    REMOVE_STAGE="service_stop_failed"
    web_service_operation service_stop "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME" ||
      die "Privileged service stop failed."
    REMOVE_STAGE="service_uninstall_failed"
    web_service_operation service_uninstall "$REPO" "$RUNNER_DIR" "$RUNNER_NAME" "$SERVICE_NAME" ||
      die "Privileged service uninstall failed."
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
  echo "==> Stopping service..."
  if ! sudo ./svc.sh stop; then
    echo "WARNING: Service stop failed; continuing to service uninstall in case it is already stopped." >&2
  fi
  echo "==> Uninstalling service..."
  uninstall_service_safely
fi

echo "==> Removing runner registration from GitHub..."
if [[ "$WEB_MODE" == "1" ]]; then
  REMOVE_STAGE="config_remove_failed"
  if ( exec {RESULT_FD}>&-; ./config.sh remove --token "$TOKEN" >/dev/null 2>&1 ); then
    unset TOKEN
  else
    REMOVE_EXIT="$?"
    # Bash's 128+signal convention cannot distinguish a killed child from
    # a normal program deliberately exiting with the same numeric status.
    # All such ambiguous statuses are conservatively unknown.
    if (( REMOVE_EXIT >= 128 )); then
      REMOVE_STAGE="unknown_failed"
      REMOVE_EXIT="unknown"
    fi
    unset TOKEN
    die "Runner registration removal failed or remains uncertain."
  fi
else
  ./config.sh remove --token "$TOKEN"
  unset TOKEN
fi

REMOVE_STAGE="local_cleanup_failed"
cd "$RUNNER_BASE_DIR"
case "$RUNNER_DIR" in "$RUNNER_BASE_DIR"/actions-runner-*) ;; *) die "Safety check failed; refusing to delete unexpected path: $RUNNER_DIR" ;; esac

echo "==> Deleting local runner directory..."
rm -rf -- "$RUNNER_DIR"

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
