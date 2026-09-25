#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

required_commands=(
  bash jq rsync find du sha256sum df timeout date awk sed tr wc
  mkdir mv rm sleep grep stat flock mktemp sort python3 base64
)

for cmd in "${required_commands[@]}"; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "FAIL: required test prerequisite missing: $cmd" >&2
    exit 1
  fi
done

echo "Checking shell syntax..."
while IFS= read -r script; do
  bash -n "$script"
done < <(find "$ROOT/scripts" "$ROOT/hooks" "$ROOT/tests" "$ROOT/.github/actions" -type f -name '*.sh' -print | sort)

tests=(
  tests/test-pure.sh
  tests/test-failure-paths.sh
  tests/test-archive-common.sh
  tests/test-register-archive.sh
  tests/test-archive-hook.sh
  tests/test-archive-hook-failures.sh
  tests/test-enable-local-archive.sh
  tests/test-cleanup-local-artifacts.sh
  tests/test-migrate-github-artifacts.sh
  tests/test-migrate-artifact-safety.sh
  tests/test-summary-action.sh
)

for test_script in "${tests[@]}"; do
  echo
  echo "============================================================"
  echo "RUN: $test_script"
  bash "$ROOT/$test_script"
done

echo
echo "PASS: all github-runner-tools tests"
