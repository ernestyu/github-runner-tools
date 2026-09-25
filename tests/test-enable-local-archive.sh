#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/enable-local-archive.sh"
fail() { echo "FAIL: $*" >&2; exit 1; }

for cmd in jq; do command -v "$cmd" >/dev/null 2>&1 || { echo "SKIP: $cmd not installed"; exit 0; }; done

TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
BASE="$TMP/runners"
ARCHIVE="$TMP/archive"
HOOK="$TMP/archive-job-completed.sh"
BIN="$TMP/bin"
mkdir -p "$BASE/actions-runner-owner--repo" "$ARCHIVE" "$BIN"
cat > "$BASE/actions-runner-owner--repo/.runner" <<'EOF'
{"agentName":"local-ci-owner--repo","gitHubUrl":"https://github.com/owner/repo"}
EOF
printf '%s\n' 'actions.runner.owner-repo.service' > "$BASE/actions-runner-owner--repo/.service"
printf '%s\n' 'PATH=/usr/bin' > "$BASE/actions-runner-owner--repo/.env"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$HOOK"
chmod +x "$HOOK"

cat > "$TMP/archive.conf" <<EOF
ARCHIVE_ROOT=$ARCHIVE
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
EOF

cat > "$BIN/systemctl" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "restart" ]]; then
  printf '%s\n' "$2" >> "$MOCK_RESTARTS"
  exit 0
fi
if [[ "$1" == "is-active" ]]; then exit 0; fi
exit 0
MOCK
cat > "$BIN/sudo" <<'MOCK'
#!/usr/bin/env bash
exec "$@"
MOCK
chmod +x "$BIN/systemctl" "$BIN/sudo"
export PATH="$BIN:$PATH"
export MOCK_RESTARTS="$TMP/restarts"

COMMON_ENV=(GRT_TEST_MODE=1 GITHUB_ACTIONS=false GRT_TEST_HOOK_PATH="$HOOK" GRT_TEST_CONFIG="$TMP/archive.conf" RUNNER_BASE_DIR="$BASE")

env "${COMMON_ENV[@]}" bash "$SCRIPT" --dry-run >/dev/null
if grep -q '^ACTIONS_RUNNER_HOOK_JOB_COMPLETED=' "$BASE/actions-runner-owner--repo/.env"; then
  fail "dry-run modified runner .env"
fi
[[ ! -e "$MOCK_RESTARTS" ]] || fail "dry-run restarted service"

env "${COMMON_ENV[@]}" bash "$SCRIPT" --apply >/dev/null
grep -qx "ACTIONS_RUNNER_HOOK_JOB_COMPLETED=$HOOK" "$BASE/actions-runner-owner--repo/.env" || fail "apply did not configure hook"
grep -qx 'PATH=/usr/bin' "$BASE/actions-runner-owner--repo/.env" || fail "apply lost unrelated .env line"
grep -qx 'actions.runner.owner-repo.service' "$MOCK_RESTARTS" || fail "exact runner service not restarted"

# Re-applying exact value must be idempotent.
env "${COMMON_ENV[@]}" bash "$SCRIPT" --apply >/dev/null
[[ "$(grep -c '^ACTIONS_RUNNER_HOOK_JOB_COMPLETED=' "$BASE/actions-runner-owner--repo/.env")" -eq 1 ]] || fail "idempotent apply duplicated hook setting"

# A conflicting completed hook must never be overwritten.
CONFLICT="$TMP/conflict"
mkdir -p "$CONFLICT/actions-runner-owner--repo"
cp "$BASE/actions-runner-owner--repo/.runner" "$CONFLICT/actions-runner-owner--repo/.runner"
cp "$BASE/actions-runner-owner--repo/.service" "$CONFLICT/actions-runner-owner--repo/.service"
printf '%s\n' 'ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/other/hook.sh' > "$CONFLICT/actions-runner-owner--repo/.env"
if env GRT_TEST_MODE=1 GITHUB_ACTIONS=false GRT_TEST_HOOK_PATH="$HOOK" GRT_TEST_CONFIG="$TMP/archive.conf" RUNNER_BASE_DIR="$CONFLICT" bash "$SCRIPT" --apply >/dev/null 2>&1; then
  fail "conflicting hook was accepted"
fi
grep -qx 'ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/other/hook.sh' "$CONFLICT/actions-runner-owner--repo/.env" || fail "conflicting hook was overwritten"

# A directory that merely looks like a runner must be ignored.
mkdir -p "$BASE/actions-runner-not-real"
env "${COMMON_ENV[@]}" bash "$SCRIPT" --dry-run >/dev/null

echo "PASS: existing-runner archive migration tests"
