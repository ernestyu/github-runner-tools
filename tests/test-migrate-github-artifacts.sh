#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/migrate-github-artifacts.sh"
fail() { echo "FAIL: $*" >&2; exit 1; }

for cmd in python3 unzip base64 jq; do command -v "$cmd" >/dev/null 2>&1 || { echo "SKIP: $cmd not installed"; exit 0; }; done

TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
ARCHIVE="$TMP/archive"
MOCKBIN="$TMP/bin"
mkdir -p "$ARCHIVE" "$MOCKBIN" "$TMP/payload"
printf 'artifact-evidence\n' > "$TMP/payload/result.txt"
python3 - "$TMP/payload" "$TMP/artifact.zip" <<'PY'
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(os.path.join(src, "result.txt"), "result.txt")
PY

ARTIFACT_JSON='{"id":123,"name":"results","size_in_bytes":999999,"created_at":"2026-09-01T00:00:00Z","expired":false,"workflow_run":{"id":456}}'
ARTIFACT_B64="$(printf '%s' "$ARTIFACT_JSON" | base64 -w0)"
cat > "$MOCKBIN/gh" <<'MOCK'
#!/usr/bin/env bash
set -e
if [[ "$1" == "auth" && "$2" == "status" ]]; then exit 0; fi
if [[ "$1" != "api" ]]; then exit 2; fi
shift
if [[ "${1:-}" == "--paginate" ]]; then
  printf '%s\n' "$MOCK_ARTIFACT_B64"
  exit 0
fi
if [[ "${1:-}" == "--method" && "${2:-}" == "DELETE" ]]; then
  : > "$MOCK_DELETED"
  exit 0
fi
# Strip optional headers.
while [[ "${1:-}" == "-H" ]]; do shift 2; done
endpoint="${1:-}"
if [[ "$endpoint" == */zip ]]; then
  cat "$MOCK_ZIP"
  exit 0
fi
if [[ "$endpoint" == */actions/artifacts/123 ]]; then
  [[ -e "$MOCK_DELETED" ]] && exit 1
  printf '%s\n' '{}'
  exit 0
fi
exit 2
MOCK
chmod +x "$MOCKBIN/gh"

cat > "$TMP/archive.conf" <<EOF
ARCHIVE_ROOT=$ARCHIVE
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
EOF

export PATH="$MOCKBIN:$PATH"
export MOCK_ARTIFACT_B64="$ARTIFACT_B64"
export MOCK_ZIP="$TMP/artifact.zip"
export MOCK_DELETED="$TMP/deleted"

GRT_TEST_MODE=1 GITHUB_ACTIONS=false GRT_TEST_CONFIG="$TMP/archive.conf" bash "$SCRIPT" owner/repo >/dev/null
MANIFEST="$ARCHIVE/owner/repo/456/github_artifacts/artifact_123--results/artifact-manifest.json"
[[ -f "$MANIFEST" ]] || fail "artifact manifest missing"
[[ "$(jq -r '.verification_status' "$MANIFEST")" == "PASS" ]] || fail "verification did not pass"
[[ "$(jq -r '.remote_deleted' "$MANIFEST")" == "false" ]] || fail "safe default deleted remote artifact"
[[ -f "$ARCHIVE/owner/repo/456/github_artifacts/artifact_123--results/payload/result.txt" ]] || fail "payload missing"
[[ ! -e "$MOCK_DELETED" ]] || fail "remote deletion occurred without explicit flag"

GRT_TEST_MODE=1 GITHUB_ACTIONS=false GRT_TEST_CONFIG="$TMP/archive.conf" bash "$SCRIPT" --delete-after-verified owner/repo >/dev/null
[[ -e "$MOCK_DELETED" ]] || fail "explicit remote deletion was not attempted"
[[ "$(jq -r '.remote_deleted' "$MANIFEST")" == "true" ]] || fail "manifest did not record remote deletion"

echo "PASS: GitHub artifact migration tests"
