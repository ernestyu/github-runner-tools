#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/migrate-github-artifacts.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

for cmd in python3 base64 jq; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "SKIP: $cmd not installed"; exit 0; }
done

TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
ARCHIVE="$TMP/archive"
BIN="$TMP/bin"
mkdir -p "$ARCHIVE" "$BIN"

python3 - "$TMP/malicious.zip" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("../escape.txt", "must-not-escape")
PY

ARTIFACT_JSON='{"id":124,"name":"bad","size_in_bytes":10,"created_at":"2026-09-01T00:00:00Z","expired":false,"workflow_run":{"id":457}}'
ARTIFACT_B64="$(printf '%s' "$ARTIFACT_JSON" | base64 -w0)"

cat > "$BIN/gh" <<'MOCK'
#!/usr/bin/env bash
set -e
if [[ "$1" == "auth" && "$2" == "status" ]]; then exit 0; fi
if [[ "$1" != "api" ]]; then exit 2; fi
shift
if [[ "${1:-}" == "--paginate" ]]; then
  printf '%s\n' "$MOCK_ARTIFACT_B64"
  exit 0
fi
while [[ "${1:-}" == "-H" ]]; do shift 2; done
if [[ "${1:-}" == */zip ]]; then
  cat "$MOCK_ZIP"
  exit 0
fi
exit 2
MOCK
chmod +x "$BIN/gh"

cat > "$TMP/archive.conf" <<EOF
ARCHIVE_ROOT=$ARCHIVE
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
EOF

if PATH="$BIN:$PATH"   MOCK_ARTIFACT_B64="$ARTIFACT_B64"   MOCK_ZIP="$TMP/malicious.zip"   GRT_TEST_MODE=1   GITHUB_ACTIONS=false   GRT_TEST_CONFIG="$TMP/archive.conf"   bash "$SCRIPT" owner/repo >/dev/null 2>&1; then
  fail "malicious traversal ZIP was accepted"
fi

[[ ! -e "$TMP/escape.txt" ]] || fail "malicious ZIP escaped extraction root"
if find "$ARCHIVE" -name artifact-manifest.json -type f -print -quit | grep -q .; then
  fail "malicious ZIP produced a finalized artifact manifest"
fi

echo "PASS: GitHub artifact migration safety tests"
