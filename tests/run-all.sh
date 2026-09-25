#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

tests=(
  tests/test-pure.sh
  tests/test-failure-paths.sh
  tests/test-archive-common.sh
  tests/test-register-archive.sh
  tests/test-archive-hook.sh
  tests/test-enable-local-archive.sh
  tests/test-cleanup-local-artifacts.sh
  tests/test-migrate-github-artifacts.sh
)

for test_script in "${tests[@]}"; do
  echo
  echo "============================================================"
  echo "RUN: $test_script"
  bash "$ROOT/$test_script"
done

echo
echo "PASS: all available github-runner-tools tests"
