#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/archive-common.sh
source "$ROOT/scripts/lib/archive-common.sh"

CONFIG="/etc/github-runner-tools/archive.conf"
if [[ "${GRT_TEST_MODE:-0}" == "1" && "${GITHUB_ACTIONS:-}" != "true" ]]; then
  CONFIG="${GRT_TEST_CONFIG:-$CONFIG}"
fi

DELETE_AFTER=0
DOWNLOAD_ONLY=0

usage() {
  cat <<'USAGE'
Usage:
  migrate-github-artifacts.sh [--download-only|--verify] [--delete-after-verified] OWNER/REPO

Default: download + verify, no remote deletion.
Requires an authenticated GitHub CLI session with suitable Actions permissions.
USAGE
}

die() { echo "ERROR: $*" >&2; exit 1; }

POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --download-only) DOWNLOAD_ONLY=1; shift ;;
    --verify) DOWNLOAD_ONLY=0; shift ;;
    --delete-after-verified) DELETE_AFTER=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "Unknown option: $1" ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done

set -- "${POSITIONAL[@]}"
[[ $# -eq 1 ]] || { usage; exit 1; }

REPOSITORY="$1"
grt_parse_repository "$REPOSITORY" || die "Repository must be valid OWNER/REPO."
(( DELETE_AFTER == 0 || DOWNLOAD_ONLY == 0 )) || die "--delete-after-verified cannot be combined with --download-only."

for cmd in gh jq python3 stat mktemp sha256sum base64 grep sed tr awk date; do
  grt_require_command "$cmd" || exit 1
done

gh auth status >/dev/null 2>&1 || die "GitHub CLI is not authenticated."
grt_load_archive_config "$CONFIG" || die "Archive config missing or invalid."
[[ -d "$ARCHIVE_ROOT" && ! -L "$ARCHIVE_ROOT" && -w "$ARCHIVE_ROOT" ]] || die "Archive root unavailable or unsafe."
ARCHIVE_ROOT="$(grt_canonical_dir "$ARCHIVE_ROOT")"
grt_is_world_writable "$ARCHIVE_ROOT" || die "Archive root must not be world-writable."

safe_extract_zip() {
  local zip="$1" dest="$2"
  python3 - "$zip" "$dest" <<'PY'
import os
import pathlib
import shutil
import stat
import sys
import zipfile

zip_path = sys.argv[1]
dest = os.path.realpath(sys.argv[2])

with zipfile.ZipFile(zip_path) as zf:
    for info in zf.infolist():
        raw = info.filename.replace("\\", "/")
        p = pathlib.PurePosixPath(raw)
        if p.is_absolute() or ".." in p.parts:
            raise SystemExit(f"unsafe artifact ZIP path: {info.filename}")

        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise SystemExit(f"symlink entries are not accepted in migrated artifact ZIPs: {info.filename}")

        parts = [x for x in p.parts if x not in ("", ".")]
        target = os.path.realpath(os.path.join(dest, *parts))
        if os.path.commonpath([dest, target]) != dest:
            raise SystemExit(f"artifact ZIP path escapes payload root: {info.filename}")

        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue

        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info, "r") as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        if mode:
            os.chmod(target, mode & 0o777)

    bad = zf.testzip()
    if bad is not None:
        raise SystemExit(f"artifact ZIP CRC verification failed at: {bad}")
PY
}

delete_remote_artifact() {
  local id="$1" manifest="$2" remaining
  gh api --method DELETE "repos/$REPOSITORY/actions/artifacts/$id"

  if ! remaining="$(gh api --paginate "repos/$REPOSITORY/actions/artifacts?per_page=100" \
    --jq ".artifacts[] | select(.id == $id) | .id")"; then
    die "Could not verify remote deletion for artifact $id."
  fi
  [[ -z "$remaining" ]] || die "Remote artifact $id still exists after delete request."

  local tmpm
  tmpm="$(mktemp)"
  jq '.remote_deleted=true' "$manifest" > "$tmpm"
  mv "$tmpm" "$manifest"
}

JSON="$(mktemp)"
trap 'rm -f -- "$JSON"' EXIT

gh api --paginate "repos/$REPOSITORY/actions/artifacts?per_page=100"   --jq '.artifacts[] | @base64' > "$JSON"

count=0
while IFS= read -r row; do
  [[ -n "$row" ]] || continue

  artifact="$(printf '%s' "$row" | base64 -d)"
  id="$(jq -r '.id' <<<"$artifact")"
  name="$(jq -r '.name' <<<"$artifact")"
  run_id="$(jq -r '.workflow_run.id // empty' <<<"$artifact")"
  size="$(jq -r '.size_in_bytes // 0' <<<"$artifact")"
  created="$(jq -r '.created_at // empty' <<<"$artifact")"
  digest="$(jq -r '.digest // empty' <<<"$artifact")"
  expired="$(jq -r '.expired // false' <<<"$artifact")"

  [[ "$expired" != "true" ]] || {
    echo "SKIP expired artifact $id $name"
    continue
  }

  [[ "$run_id" =~ ^[0-9]+$ && "$id" =~ ^[0-9]+$ ]] || die "Invalid artifact metadata for id=$id"
  [[ "$size" =~ ^[0-9]+$ ]] || die "Invalid artifact size for id=$id"

  safe_name="$(grt_sanitize_component "$name")" || safe_name="artifact"

  owner_root="$(grt_ensure_child_dir "$ARCHIVE_ROOT" "$GRT_OWNER_PATH")" || die "Unsafe owner archive path."
  repo_root="$(grt_ensure_child_dir "$owner_root" "$GRT_REPO_PATH")" || die "Unsafe repository archive path."
  run_root="$(grt_ensure_child_dir "$repo_root" "$run_id")" || die "Unsafe run archive path."
  github_root="$(grt_ensure_child_dir "$run_root" "github_artifacts")" || die "Unsafe GitHub artifact path."

  dest="$github_root/artifact_${id}--${safe_name}"
  manifest="$dest/artifact-manifest.json"

  if [[ -e "$manifest" ]]; then
    status="$(jq -r '.verification_status // empty' "$manifest" 2>/dev/null || true)"
    remote_deleted="$(jq -r '.remote_deleted // false' "$manifest" 2>/dev/null || true)"
    if [[ "$status" == "PASS" ]]; then
      if (( DELETE_AFTER == 1 )) && [[ "$remote_deleted" != "true" ]]; then
        echo "Using existing verified local copy for remote deletion: artifact $id $name"
        delete_remote_artifact "$id" "$manifest"
        echo "Deleted remote artifact $id after existing local verification."
      else
        echo "SKIP already verified artifact $id $name"
      fi
      continue
    fi
    die "Existing ambiguous migration directory: $dest"
  fi

  [[ ! -e "$dest" ]] || die "Existing ambiguous migration directory: $dest"

  tmpdir="$(mktemp -d "$github_root/.artifact_${id}.tmp.XXXXXX")"
  zip="$tmpdir/artifact.zip"
  payload="$tmpdir/payload"
  mkdir "$payload"

  echo "Downloading artifact $id $name (run $run_id)..."
  if ! gh api -H "Accept: application/vnd.github+json"     "repos/$REPOSITORY/actions/artifacts/$id/zip" > "$zip"; then
    rm -rf -- "$tmpdir"
    die "Artifact $id download failed."
  fi

  zip_bytes="$(stat -c '%s' "$zip")"
  [[ "$zip_bytes" -gt 0 ]] || {
    rm -rf -- "$tmpdir"
    die "Artifact $id download is empty."
  }

  if ! safe_extract_zip "$zip" "$payload"; then
    rm -rf -- "$tmpdir"
    die "Artifact $id safe extraction/integrity verification failed."
  fi

  verify_status="NOT_REQUESTED"
  digest_verified=false

  if (( DOWNLOAD_ONLY == 0 )); then
    if [[ -n "$digest" ]]; then
      [[ "$digest" == sha256:* ]] || {
        rm -rf -- "$tmpdir"
        die "Artifact $id exposes an unsupported digest format: $digest"
      }
      expected="${digest#sha256:}"
      actual="$(sha256sum "$zip" | awk '{print $1}')"
      [[ "$actual" == "$expected" ]] || {
        rm -rf -- "$tmpdir"
        die "Artifact $id digest verification failed."
      }
      digest_verified=true
    fi
    verify_status="PASS"
  fi

  downloaded="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir "$dest"
  mv "$payload" "$dest/payload"

  jq -n     --argjson artifact_id "$id"     --arg artifact_name "$name"     --arg repository "$REPOSITORY"     --arg run_id "$run_id"     --argjson original_size "$size"     --argjson downloaded_zip_size "$zip_bytes"     --arg created_at "$created"     --arg downloaded_at_utc "$downloaded"     --arg verification_status "$verify_status"     --arg digest "$digest"     --argjson digest_verified "$digest_verified"     --arg local_payload_path "$dest/payload"     --argjson remote_deleted false     '{
      artifact_id:$artifact_id,
      artifact_name:$artifact_name,
      repository:$repository,
      run_id:$run_id,
      original_size:$original_size,
      downloaded_zip_size:$downloaded_zip_size,
      created_at:$created_at,
      downloaded_at_utc:$downloaded_at_utc,
      verification_status:$verification_status,
      digest:$digest,
      digest_verified:$digest_verified,
      local_payload_path:$local_payload_path,
      remote_deleted:$remote_deleted
    }' > "$manifest"

  rm -rf -- "$tmpdir"

  if (( DELETE_AFTER == 1 )); then
    [[ "$verify_status" == "PASS" ]] || die "Internal safety error: remote deletion requested without verification PASS."
    delete_remote_artifact "$id" "$manifest"
    echo "Deleted remote artifact $id after local verification."
  fi

  count=$((count + 1))
done < "$JSON"

echo "Artifact migration complete: repository=$REPOSITORY processed=$count delete_after_verified=$DELETE_AFTER"
