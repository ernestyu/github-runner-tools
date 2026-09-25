#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

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
echo "PASS: all available github-runner-tools tests"
