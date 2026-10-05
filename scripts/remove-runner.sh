#!/usr/bin/env bash
set -Eeuo pipefail

MAX_LOCAL_ID_LENGTH=64
HASH_LENGTH=8
TOKEN=""

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
trap cleanup EXIT

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
  local file="$1" line extra

  [[ -f "$file" && ! -L "$file" && -r "$file" ]] || return 1

  IFS= read -r line < "$file" || [[ -n "$line" ]] || return 1
  line="${line%
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
for cmd in jq sudo id awk tr sed sha256sum cut find systemctl; do require_command "$cmd"; done

CLI_BASE_DIR=""; RECOVER_LOCAL=0; POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir) [[ $# -ge 2 ]] || die "--base-dir requires a value"; CLI_BASE_DIR="$2"; shift 2 ;;
    --recover-local) RECOVER_LOCAL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    -*) die "Unknown option: $1" ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
set -- "${POSITIONAL[@]}"
[[ $# -eq 1 ]] || { usage; exit 1; }
REPO="$1"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "Repository must be in OWNER/REPO form."
OWNER="${REPO%%/*}"; REPO_NAME="${REPO##*/}"
SAFE_REPO="$(sanitize_component "$REPO_NAME")" || die "Could not derive safe repository name."
LOCAL_ID="$(make_local_id "$OWNER" "$REPO_NAME")" || die "Could not derive local identity."
RUNNER_USER="$(id -un)"; USER_HOME="$(resolve_home "$RUNNER_USER")" || die "Could not resolve home for $RUNNER_USER"
RUNNER_BASE_DIR="${CLI_BASE_DIR:-${RUNNER_BASE_DIR:-$USER_HOME}}"
[[ -d "$RUNNER_BASE_DIR" && -x "$RUNNER_BASE_DIR" && -r "$RUNNER_BASE_DIR" && -w "$RUNNER_BASE_DIR" ]] || die "RUNNER_BASE_DIR is not accessible and writable: $RUNNER_BASE_DIR"
RUNNER_BASE_DIR="$(canonicalize_existing_dir "$RUNNER_BASE_DIR")" || die "Could not canonicalize RUNNER_BASE_DIR: $RUNNER_BASE_DIR"

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
  EXPECTED_NEW_DIR="$(canonicalize_existing_dir "$NEW_DIR")" ||
    die "Could not canonicalize expected recovery target: $NEW_DIR"

  SERVICE_NAME="$(validate_recovery_identity "$RUNNER_DIR" "$EXPECTED_NEW_DIR" "$OWNER" "$REPO_NAME")"

  cd "$RUNNER_DIR"
  SERVICE_STATE="$(runner_service_state)"
  case "$SERVICE_STATE" in
    present|absent) ;;
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

  CONFIRM="$(read_tty_line "Type RECOVER-REMOVE to uninstall local residue and delete this runner directory: ")"
  [[ "$CONFIRM" == "RECOVER-REMOVE" ]] || die "Cancelled."

  if [[ "$SERVICE_STATE" == "present" ]]; then
    echo "==> Stopping service..."
    if ! sudo ./svc.sh stop; then
      echo "WARNING: Service stop failed; continuing to service uninstall so the final systemd state can be checked." >&2
    fi

    echo "==> Uninstalling service..."
    uninstall_service_safely
  else
    echo "==> Systemd service is already absent; continuing."
  fi

  FINAL_SERVICE_STATE="$(runner_service_state)"
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

TOKEN="$(read_tty_line "Paste GitHub removal token: " 1)"
[[ -n "$TOKEN" ]] || die "Removal token cannot be empty."
CONFIRM="$(read_tty_line "Type REMOVE to unregister and delete this runner: ")"
[[ "$CONFIRM" == "REMOVE" ]] || die "Cancelled."

cd "$RUNNER_DIR"
echo "==> Stopping service..."
if ! sudo ./svc.sh stop; then
  echo "WARNING: Service stop failed; continuing to service uninstall in case it is already stopped." >&2
fi

echo "==> Uninstalling service..."
uninstall_service_safely

echo "==> Removing runner registration from GitHub..."
./config.sh remove --token "$TOKEN"
unset TOKEN

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
\r'}"
  [[ -n "$line" ]] || return 1

  if IFS= read -r extra < <(tail -n +2 "$file"); then
    [[ -z "$extra" ]] || return 1
    # A second blank line is still a second line and therefore invalid.
    [[ "$(wc -l < "$file")" -le 1 ]] || return 1
  fi

  # Reject embedded/newline content even when the final line has no newline.
  [[ "$(printf '%s' "$line" | wc -l)" -eq 0 ]] || return 1
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

  [[ "$runner_dir" == "$expected_dir" ]] || die "Recovery identity cannot be verified: runner directory does not match the expected owner+repository path."

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
for cmd in jq sudo id awk tr sed sha256sum cut find systemctl; do require_command "$cmd"; done

CLI_BASE_DIR=""; POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir) [[ $# -ge 2 ]] || die "--base-dir requires a value"; CLI_BASE_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    -*) die "Unknown option: $1" ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
set -- "${POSITIONAL[@]}"
[[ $# -eq 1 ]] || { usage; exit 1; }
REPO="$1"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "Repository must be in OWNER/REPO form."
OWNER="${REPO%%/*}"; REPO_NAME="${REPO##*/}"
SAFE_REPO="$(sanitize_component "$REPO_NAME")" || die "Could not derive safe repository name."
LOCAL_ID="$(make_local_id "$OWNER" "$REPO_NAME")" || die "Could not derive local identity."
RUNNER_USER="$(id -un)"; USER_HOME="$(resolve_home "$RUNNER_USER")" || die "Could not resolve home for $RUNNER_USER"
RUNNER_BASE_DIR="${CLI_BASE_DIR:-${RUNNER_BASE_DIR:-$USER_HOME}}"
[[ -d "$RUNNER_BASE_DIR" && -x "$RUNNER_BASE_DIR" && -r "$RUNNER_BASE_DIR" && -w "$RUNNER_BASE_DIR" ]] || die "RUNNER_BASE_DIR is not accessible and writable: $RUNNER_BASE_DIR"
RUNNER_BASE_DIR="$(canonicalize_existing_dir "$RUNNER_BASE_DIR")" || die "Could not canonicalize RUNNER_BASE_DIR: $RUNNER_BASE_DIR"

NEW_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"
LEGACY_DIR="$RUNNER_BASE_DIR/actions-runner-$SAFE_REPO"
RUNNER_DIR=""
MODE=""

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

TOKEN="$(read_tty_line "Paste GitHub removal token: " 1)"
[[ -n "$TOKEN" ]] || die "Removal token cannot be empty."
CONFIRM="$(read_tty_line "Type REMOVE to unregister and delete this runner: ")"
[[ "$CONFIRM" == "REMOVE" ]] || die "Cancelled."

cd "$RUNNER_DIR"
echo "==> Stopping service..."
if ! sudo ./svc.sh stop; then
  echo "WARNING: Service stop failed; continuing to service uninstall in case it is already stopped." >&2
fi

echo "==> Uninstalling service..."
uninstall_service_safely

echo "==> Removing runner registration from GitHub..."
./config.sh remove --token "$TOKEN"
unset TOKEN

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
