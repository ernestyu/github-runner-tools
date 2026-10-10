#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

required_commands=(
  bash jq rsync find du sha256sum df timeout date awk sed tr wc
  mkdir mv rm sleep grep stat flock mktemp sort python3 base64 sudo script
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

echo "Checking Python syntax..."
python3 -m py_compile "$ROOT"/web/*.py "$ROOT"/tests/test-web-management.py "$ROOT"/tests/test-web-confirmation-gate-a.py "$ROOT"/tests/test-web-official-remove.py "$ROOT"/tests/test-web-service-record.py "$ROOT"/tests/test-runner-version-compatibility.py $ROOT"/tests/test-runner-lifecycle-authority.py "$ROOT"/web/runner_lifecycle_authority.py "$ROOT"/scripts/web-service-record.py

tests=(
  tests/test-pure.sh
  tests/test-failure-paths.sh
  tests/test-recover-local-removal.sh
  tests/test-web-contracts.sh
  tests/test-web-setup-apply.sh
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

echo
echo "============================================================"
echo "RUN: tests/test-web-management.py"
python3 "$ROOT/tests/test-web-management.py"

echo
 echo "============================================================"
echo "RUN: tests/test-web-confirmation-gate-a.py"
python3 "$ROOT/tests/test-web-confirmation-gate-a.py"

echo
 echo "============================================================"
echo "RUN: tests/test-web-official-remove.py"
python3 "$ROOT/tests/test-web-official-remove.py"

echo
echo "============================================================"
echo "RUN: tests/test-web-service-record.py"
python3 "$ROOT/tests/test-web-service-record.py"
echo "RUN: tests/test-web-runner-cleanup.py"
python3 "$ROOT/tests/test-web-runner-cleanup.py"

echo "RUN: tests/test-runner-version-compatibility.py"
python3 "$ROOT/tests/test-runner-version-compatibility.py"
echo "RUN: tests/test-runner-lifecycle-authority.py (root-isolated)"
if [[ "$(id -u)" == "0" ]]; then
  PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/test-runner-lifecycle-authority.py"
else
  sudo -n env PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/test-runner-lifecycle-authority.py"
fi

for test_script in "${tests[@]}"; do
  echo
  echo "============================================================"
  echo "RUN: $test_script"
  bash "$ROOT/$test_script"
done

echo
echo "PASS: all github-runner-tools tests"
