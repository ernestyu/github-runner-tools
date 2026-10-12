#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TAG="${1:-}"
OUT="${2:-$ROOT/dist}"

[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9._-]+)?$ ]] || {
  echo "Usage: build-release.sh vX.Y.Z [OUT_DIR]" >&2
  exit 2
}

for cmd in tar sha256sum sed install mktemp; do
  command -v "$cmd" >/dev/null || {
    echo "Missing command: $cmd" >&2
    exit 1
  }
done

rm -rf -- "$OUT"
mkdir -p "$OUT"
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT

mkdir -p "$stage/scripts/lib" "$stage/web" "$stage/hooks"

runtime_scripts=(
  fresh-install.sh
  install-state-validator.py
  install-auth-recovery.py
  install-lan.sh
  setup-local-archive.sh
  register-runner.sh
  remove-runner.sh
  status-runners.sh
  cli-operational-logger.py
  web-runner-cleanup.py
  web-service-record.py
)

for name in "${runtime_scripts[@]}"; do
  install -m 0755 "$ROOT/scripts/$name" "$stage/scripts/$name"
done

install -m 0644   "$ROOT/scripts/lib/archive-common.sh"   "$stage/scripts/lib/archive-common.sh"
install -m 0755   "$ROOT/hooks/archive-job-completed.sh"   "$stage/hooks/archive-job-completed.sh"

for src in "$ROOT"/web/*.py; do
  install -m 0755 "$src" "$stage/web/${src##*/}"
done

archive="$OUT/github-runner-tools-$TAG.tar.gz"
tar -C "$stage" -czf "$archive" .
sha="$(sha256sum "$archive" | awk '{print $1}')"
url="https://github.com/ernestyu/github-runner-tools/releases/download/$TAG/github-runner-tools-$TAG.tar.gz"

sed   -e "s|@GRT_VERSION@|$TAG|g"   -e "s|@GRT_SHA256@|$sha|g"   -e "s|@GRT_ARCHIVE_URL@|$url|g"   "$ROOT/install.sh" >"$OUT/install.sh"
chmod 0755 "$OUT/install.sh"

cat >"$OUT/SHA256SUMS" <<EOF
$sha  github-runner-tools-$TAG.tar.gz
$(sha256sum "$OUT/install.sh" | awk '{print $1}')  install.sh
EOF

echo "Release payload built: $archive"
echo "Payload SHA-256: $sha"
