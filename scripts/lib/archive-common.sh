#!/usr/bin/env bash
# github-runner-tools-managed-library
# Shared helpers for the local GitHub Actions archive subsystem.
# Intended to be sourced.

GRT_DEFAULT_CONFIG_FILE="/etc/github-runner-tools/archive.conf"
GRT_DEFAULT_ARCHIVE_ROOT="/srv/github-actions-archive"
GRT_DEFAULT_RETENTION_DAYS="90"
GRT_DEFAULT_MIN_FREE_PERCENT="15"
GRT_DEFAULT_COPY_TIMEOUT_SECONDS="3600"

grt_die() { echo "ERROR: $*" >&2; return 1; }
grt_warn() { echo "WARNING: $*" >&2; }
grt_require_command() { command -v "$1" >/dev/null 2>&1 || grt_die "Required command not found: $1"; }

grt_is_uint() { [[ "${1:-}" =~ ^[0-9]+$ ]]; }

grt_sanitize_component() {
  local value="${1:-}" out
  [[ -n "$value" ]] || return 1
  [[ "$value" != "." && "$value" != ".." ]] || return 1
  [[ "$value" != *"/"* ]] || return 1
  if printf '%s' "$value" | LC_ALL=C grep -q '[[:cntrl:]]'; then return 1; fi
  out="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^[._-]+//; s/[._-]+$//')"
  [[ -n "$out" && "$out" != "." && "$out" != ".." ]] || return 1
  printf '%s' "$out"
}

grt_parse_repository() {
  local repository="${1:-}" owner repo
  [[ "$repository" =~ ^[^/]+/[^/]+$ ]] || return 1
  owner="${repository%%/*}"
  repo="${repository##*/}"
  GRT_OWNER_PATH="$(grt_sanitize_component "$owner")" || return 1
  GRT_REPO_PATH="$(grt_sanitize_component "$repo")" || return 1
}

grt_validate_run_identity() {
  grt_is_uint "${GITHUB_RUN_ID:-}" || return 1
  grt_is_uint "${GITHUB_RUN_ATTEMPT:-}" || return 1
  [[ -n "${GITHUB_JOB:-}" ]] || return 1
  GRT_JOB_SAFE="$(grt_sanitize_component "$GITHUB_JOB")" || return 1
}

grt_canonical_dir() {
  local dir="$1"
  [[ -d "$dir" ]] || return 1
  (cd -- "$dir" && pwd -P)
}

grt_assert_beneath() {
  local root="$1" path="$2"
  [[ "$root" = /* && "$path" = /* ]] || return 1
  case "$path" in
    "$root"|"$root"/*) return 0 ;;
    *) return 1 ;;
  esac
}

grt_ensure_child_dir() {
  local parent="$1" child="$2" path canon
  [[ "$parent" = /* && -d "$parent" && ! -L "$parent" ]] || return 1
  [[ -n "$child" && "$child" != "." && "$child" != ".." && "$child" != *"/"* ]] || return 1
  path="$parent/$child"
  [[ ! -L "$path" ]] || return 1
  if [[ ! -e "$path" ]]; then
    mkdir -- "$path" || return 1
  fi
  [[ -d "$path" && ! -L "$path" ]] || return 1
  canon="$(grt_canonical_dir "$path")" || return 1
  grt_assert_beneath "$parent" "$canon" || return 1
  printf '%s' "$canon"
}

grt_is_world_writable() {
  local path="$1" mode other
  mode="$(stat -c '%a' "$path" 2>/dev/null)" || return 1
  other="${mode: -1}"
  [[ "$other" =~ ^[0-7]$ ]] || return 1
  (( (10#$other & 2) == 0 ))
}

grt_load_archive_config() {
  local file="${1:-$GRT_DEFAULT_CONFIG_FILE}" line key value
  ARCHIVE_ROOT=""
  RETENTION_DAYS=""
  MIN_FREE_PERCENT=""
  COPY_TIMEOUT_SECONDS=""

  [[ -r "$file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || return 1
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      ARCHIVE_ROOT) ARCHIVE_ROOT="$value" ;;
      RETENTION_DAYS) RETENTION_DAYS="$value" ;;
      MIN_FREE_PERCENT) MIN_FREE_PERCENT="$value" ;;
      COPY_TIMEOUT_SECONDS) COPY_TIMEOUT_SECONDS="$value" ;;
      *) return 1 ;;
    esac
  done < "$file"

  [[ -n "$ARCHIVE_ROOT" && "$ARCHIVE_ROOT" = /* && "$ARCHIVE_ROOT" != "/" ]] || return 1
  grt_is_uint "$RETENTION_DAYS" || return 1
  grt_is_uint "$MIN_FREE_PERCENT" || return 1
  grt_is_uint "$COPY_TIMEOUT_SECONDS" || return 1
  (( RETENTION_DAYS >= 1 )) || return 1
  (( MIN_FREE_PERCENT >= 1 && MIN_FREE_PERCENT <= 99 )) || return 1
  (( COPY_TIMEOUT_SECONDS >= 1 )) || return 1
}

grt_disk_stats() {
  local root="$1" line blocks used avail
  line="$(df -Pk "$root" | awk 'NR==2 {print $2" "$3" "$4}')" || return 1
  read -r blocks used avail <<< "$line"
  grt_is_uint "$blocks" && grt_is_uint "$used" && grt_is_uint "$avail" || return 1
  (( blocks > 0 )) || return 1
  GRT_FS_TOTAL_BYTES=$((blocks * 1024))
  GRT_FS_USED_BYTES=$((used * 1024))
  GRT_FS_FREE_BYTES=$((avail * 1024))
  GRT_FS_FREE_PERCENT=$((avail * 100 / blocks))
}

grt_archive_uri_for() {
  local owner="$1" repo="$2" run="$3" attempt="${4:-}" job="${5:-}"
  local uri="archive://$owner/$repo/$run"
  [[ -n "$attempt" ]] && uri="$uri/attempt_$attempt"
  [[ -n "$job" ]] && uri="$uri/$job"
  printf '%s' "$uri"
}

grt_read_runner_env_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 1
  awk -F= -v k="$key" '$1 == k {sub(/^[^=]*=/, ""); print; found=1} END {exit !found}' "$file"
}

grt_set_runner_env_value() {
  local file="$1" key="$2" value="$3" tmp current=""
  [[ -f "$file" ]] || : > "$file"
  if current="$(grt_read_runner_env_value "$file" "$key" 2>/dev/null)"; then
    [[ "$current" == "$value" ]] || return 2
    return 0
  fi
  tmp="$(mktemp "${file}.tmp.XXXXXX")" || return 1
  cat -- "$file" > "$tmp"
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  chmod --reference="$file" "$tmp" 2>/dev/null || true
  mv -f -- "$tmp" "$file"
}

grt_iso_to_epoch() {
  date -u -d "$1" +%s 2>/dev/null
}
