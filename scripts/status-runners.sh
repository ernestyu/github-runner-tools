#!/usr/bin/env bash
set -Eeuo pipefail

MAX_LOCAL_ID_LENGTH=64
HASH_LENGTH=8

die() { echo "ERROR: $*" >&2; exit 1; }
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
normalize_repo_url() {
  local value="$1"
  value="${value%/}"
  value="${value%.git}"
  printf '%s' "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
}
repository_from_metadata() {
  local file="$1" url path
  [[ -f "$file" && ! -L "$file" ]] || return 1
  url="$(jq -r '.gitHubUrl // .serverUrl // empty' "$file" 2>/dev/null)" || return 1
  url="${url%/}"; url="${url%.git}"
  [[ "$url" == https://github.com/* ]] || return 1
  path="${url#https://github.com/}"
  [[ "$path" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || return 1
  printf '%s' "$path"
}
strict_service_name() {
  local file="$1" -a lines=()
  [[ -f "$file" && ! -L "$file" && -r "$file" ]] || return 1
  mapfile -t lines < "$file" || return 1
  [[ "${#lines[@]}" -eq 1 && -n "${lines[0]}" ]] || return 1
  printf '%s' "${lines[0]}"
}
service_state_for_dir() {
  local dir="$1" service load active
  service="$(strict_service_name "$dir/.service" 2>/dev/null)" || { printf '%s' unknown; return 0; }
  load="$(systemctl show "$service" -p LoadState --value 2>/dev/null || true)"
  case "$load" in
    not-found) printf '%s' absent; return 0 ;;
    "") printf '%s' unknown; return 0 ;;
  esac
  active="$(systemctl show "$service" -p ActiveState --value 2>/dev/null || true)"
  case "$active" in
    active) printf '%s' active ;;
    inactive|failed|activating|deactivating) printf '%s' inactive ;;
    *) printf '%s' unknown ;;
  esac
}
repo_from_new_dir_residue() {
  local dir="$1" base local_id owner repo reconstructed service lower_service scope
  base="$(basename -- "$dir")"
  [[ "$base" == actions-runner-* ]] || return 1
  local_id="${base#actions-runner-}"
  # Only the reversible non-truncated form is eligible. More than one "--"
  # separator is ambiguous (component contains "--" or truncation hash suffix).
  [[ "$local_id" == *--* ]] || return 1
  [[ "${local_id#*--}" != *--* ]] || return 1
  owner="${local_id%%--*}"
  repo="${local_id#*--}"
  [[ -n "$owner" && -n "$repo" ]] || return 1
  reconstructed="$owner/$repo"
  [[ "$(make_local_id "$owner" "$repo")" == "$local_id" ]] || return 1
  service="$(strict_service_name "$dir/.service" 2>/dev/null)" || return 1
  lower_service="$(printf '%s' "$service" | tr '[:upper:]' '[:lower:]')"
  scope="$(printf '%s-%s' "$owner" "$repo" | sed -E 's/[^0-9A-Za-z._-]/-/g' | tr '[:upper:]' '[:lower:]')"
  [[ "$lower_service" == "actions.runner.$scope."*".service" ]] || return 1
  [[ "${lower_service#actions.runner.$scope.}" != ".service" ]] || return 1
  printf '%s' "$reconstructed"
}

JSON_MODE=0
if [[ "${1:-}" == "--json" ]]; then JSON_MODE=1; shift; fi
[[ $# -eq 0 ]] || die "Usage: status-runners.sh [--json]"
if [[ "$JSON_MODE" == "1" ]]; then
  command -v jq >/dev/null 2>&1 || die "jq is required for --json"
  command -v systemctl >/dev/null 2>&1 || die "systemctl is required for --json"
fi

RUNNER_USER="$(id -un)"
USER_HOME="$(resolve_home "$RUNNER_USER")" || die "Could not resolve home for $RUNNER_USER"
RUNNER_BASE_DIR="${RUNNER_BASE_DIR:-$USER_HOME}"
[[ -d "$RUNNER_BASE_DIR" ]] || die "Runner base directory does not exist: $RUNNER_BASE_DIR"
RUNNER_BASE_DIR="$(canonicalize_existing_dir "$RUNNER_BASE_DIR")" || die "Could not canonicalize RUNNER_BASE_DIR: $RUNNER_BASE_DIR"

shopt -s nullglob
RUNNER_DIRS=("$RUNNER_BASE_DIR"/actions-runner-*)
shopt -u nullglob

if [[ "$JSON_MODE" == "1" ]]; then
  TMP="$(mktemp)"
  trap 'rm -f -- "$TMP"' EXIT
  for dir in "${RUNNER_DIRS[@]}"; do
    repository=""
    runner_name=""
    configured=false
    management_state="ambiguous"
    can_remove=false
    can_recover=false

    if [[ -f "$dir/.runner" && ! -L "$dir/.runner" ]]; then
      configured=true
      repository="$(repository_from_metadata "$dir/.runner" 2>/dev/null || true)"
      runner_name="$(jq -r '.agentName // empty' "$dir/.runner" 2>/dev/null || true)"
      if [[ -n "$repository" && -n "$runner_name" && -x "$dir/config.sh" && -x "$dir/svc.sh" ]]; then
        management_state="configured"
        can_remove=true
      else
        management_state="ambiguous"
      fi
    elif [[ -e "$dir/.runner" || -L "$dir/.runner" ]]; then
      management_state="ambiguous"
    else
      repository="$(repo_from_new_dir_residue "$dir" 2>/dev/null || true)"
      if [[ -n "$repository" && -x "$dir/svc.sh" ]]; then
        service_name="$(strict_service_name "$dir/.service" 2>/dev/null || true)"
        scope="$(printf '%s' "${repository/\//-}" | sed -E 's/[^0-9A-Za-z._-]/-/g')"
        prefix="actions.runner.$scope."
        suffix=".service"
        if [[ "$service_name" == "$prefix"*"$suffix" ]]; then
          runner_name="${service_name:${#prefix}:${#service_name}-${#prefix}-${#suffix}}"
        fi
        management_state="recoverable_residue"
      elif [[ -n "$(find "$dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
        management_state="incomplete"
      else
        management_state="incomplete"
      fi
    fi

    service_state="$(service_state_for_dir "$dir")"
    if [[ "$management_state" == "recoverable_residue" && "$service_state" != "unknown" ]]; then
      can_recover=true
    fi

    jq -nc       --arg repository "$repository"       --arg runner_name "$runner_name"       --arg runner_dir "$dir"       --arg service_state "$service_state"       --arg management_state "$management_state"       --argjson configured "$configured"       --argjson can_remove "$can_remove"       --argjson can_recover "$can_recover"       '{
        repository: (if $repository == "" then null else $repository end),
        runner_name: (if $runner_name == "" then null else $runner_name end),
        runner_dir: $runner_dir,
        configured: $configured,
        service_state: $service_state,
        management_state: $management_state,
        can_remove: $can_remove,
        can_recover_local: $can_recover
      }' >> "$TMP"
  done
  jq -s '.' "$TMP"
  exit 0
fi

printf 'Runner user : %s\n' "$RUNNER_USER"
printf 'Runner base : %s\n' "$RUNNER_BASE_DIR"

ARCHIVE_LIB="/usr/local/lib/github-runner-tools/archive-common.sh"
ARCHIVE_CONFIG="/etc/github-runner-tools/archive.conf"
ARCHIVE_HOOK="/usr/local/lib/github-runner-tools/hooks/archive-job-completed.sh"
echo
echo "Local artifact archive:"
echo "  config: $ARCHIVE_CONFIG"
echo "  hook  : $ARCHIVE_HOOK"
if [[ -r "$ARCHIVE_LIB" && -r "$ARCHIVE_CONFIG" ]]; then
  # shellcheck source=/dev/null
  source "$ARCHIVE_LIB"
  if grt_load_archive_config "$ARCHIVE_CONFIG" && [[ -d "$ARCHIVE_ROOT" ]]; then
    writable="no"; [[ -w "$ARCHIVE_ROOT" ]] && writable="yes"
    if grt_disk_stats "$ARCHIVE_ROOT"; then
      echo "  root                 : $ARCHIVE_ROOT"
      world_writable="yes"; grt_is_world_writable "$ARCHIVE_ROOT" && world_writable="no"
      echo "  root writable        : $writable"
      echo "  root world-writable  : $world_writable"
      echo "  filesystem total     : $GRT_FS_TOTAL_BYTES bytes"
      echo "  filesystem used      : $GRT_FS_USED_BYTES bytes"
      echo "  filesystem free      : $GRT_FS_FREE_BYTES bytes"
      echo "  free percentage      : $GRT_FS_FREE_PERCENT%"
      echo "  retention days       : $RETENTION_DAYS"
      echo "  disk guard threshold : $MIN_FREE_PERCENT%"
      echo "  copy timeout         : $COPY_TIMEOUT_SECONDS seconds"
      echo "  hook executable      : $([[ -x "$ARCHIVE_HOOK" ]] && echo yes || echo no)"
    else
      echo "  status: configured, but filesystem statistics failed"
    fi
  else
    echo "  status: config invalid or archive root missing"
  fi
else
  echo "  status: not configured"
fi
echo

if [[ ${#RUNNER_DIRS[@]} -eq 0 ]]; then echo "No actions-runner-* directories found."; exit 0; fi

for dir in "${RUNNER_DIRS[@]}"; do
  echo "============================================================"
  echo "Runner directory: $dir"
  if [[ -f "$dir/.runner" ]]; then
    if command -v jq >/dev/null 2>&1; then
      NAME="$(jq -r '.agentName // "unknown"' "$dir/.runner" 2>/dev/null || echo unknown)"
      URL="$(jq -r '.gitHubUrl // .serverUrl // "unknown"' "$dir/.runner" 2>/dev/null || echo unknown)"
      echo "Runner name     : $NAME"
      echo "GitHub URL      : $URL"
    else
      echo "Configured      : yes"
    fi
  else
    echo "Configured      : no (.runner not found)"
  fi
  if [[ -x "$dir/svc.sh" ]]; then
    echo "Service status:"
    (cd "$dir" && sudo ./svc.sh status) || true
  else
    echo "Service status  : svc.sh not found"
  fi
  echo
done

echo "============================================================"
echo "Systemd runner services:"
systemctl --type=service --all 2>/dev/null | grep actions.runner || true
