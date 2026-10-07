#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
fail() { echo "FAIL: $*" >&2; exit 1; }

TMP="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT

# status-runners.sh --json must expose the frozen machine contract without
# scraping human-readable output.
MOCKBIN="$TMP/mockbin"
BASE="$TMP/runners"
mkdir -p "$MOCKBIN" "$BASE"

cat > "$MOCKBIN/systemctl" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "show" ]]; then
  property=""
  for ((i=1; i<=$#; i++)); do
    if [[ "${!i}" == "-p" ]]; then
      j=$((i+1)); property="${!j}"
    fi
  done
  case "$property" in
    LoadState) printf '%s\n' loaded ;;
    ActiveState) printf '%s\n' active ;;
    *) printf '%s\n' "" ;;
  esac
  exit 0
fi
exit 0
MOCK
chmod +x "$MOCKBIN/systemctl"

CONFIGURED="$BASE/actions-runner-ernestyu--example"
mkdir -p "$CONFIGURED"
cat > "$CONFIGURED/.runner" <<'JSON'
{"agentName":"local-ci-ernestyu--example","gitHubUrl":"https://github.com/ernestyu/example"}
JSON
printf '%s\n' 'actions.runner.ernestyu-example.local-ci-ernestyu--example.service' > "$CONFIGURED/.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$CONFIGURED/config.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$CONFIGURED/svc.sh"
chmod +x "$CONFIGURED/config.sh" "$CONFIGURED/svc.sh"

JSON_OUT="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '
  length == 1 and
  .[0].repository == "ernestyu/example" and
  .[0].runner_name == "local-ci-ernestyu--example" and
  .[0].configured == true and
  .[0].service_state == "active" and
  .[0].management_state == "configured" and
  .[0].can_remove == true and
  .[0].can_recover_local == false
' <<<"$JSON_OUT" >/dev/null || fail "configured status JSON contract mismatch"

rm -rf -- "$CONFIGURED"
RECOVERABLE="$BASE/actions-runner-ernestyu--example"
mkdir -p "$RECOVERABLE"
printf '%s\n' 'actions.runner.ernestyu-example.custom-runner.service' > "$RECOVERABLE/.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$RECOVERABLE/svc.sh"
chmod +x "$RECOVERABLE/svc.sh"

JSON_OUT="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '
  length == 1 and
  .[0].repository == "ernestyu/example" and
  .[0].runner_name == "custom-runner" and
  .[0].configured == false and
  .[0].management_state == "recoverable_residue" and
  .[0].can_remove == false and
  .[0].can_recover_local == true
' <<<"$JSON_OUT" >/dev/null || fail "recoverable status JSON contract mismatch"

# CLI-only scripts must not install or enable Web Management.
for script in register-runner.sh remove-runner.sh status-runners.sh setup-local-archive.sh; do
  if grep -Eq 'setup-web-management|github-runner-tools-web\.service|web-dispatch\.sock|tailscale serve' "$ROOT/scripts/$script"; then
    fail "CLI-only path unexpectedly enables Web Management: $script"
  fi
done

# setup-web-management defaults to dry-run and the mutation branch is after the
# dry-run exit. Exercise it with harmless command mocks and verify no marker was
# touched by mocked privileged commands.
SETUP_MOCK="$TMP/setup-mock"
mkdir -p "$SETUP_MOCK"
MARKER="$TMP/persistent-mutation"
cat > "$SETUP_MOCK/systemctl" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "is-active" && "${2:-}" == "--quiet" && "${3:-}" == "tailscaled" ]]; then exit 0; fi
exit 0
MOCK
cat > "$SETUP_MOCK/tailscale" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
cat > "$SETUP_MOCK/sudo" <<MOCK
#!/usr/bin/env bash
touch "$MARKER"
exit 99
MOCK
chmod +x "$SETUP_MOCK/systemctl" "$SETUP_MOCK/tailscale" "$SETUP_MOCK/sudo"

PATH="$SETUP_MOCK:$PATH" bash "$ROOT/scripts/setup-web-management.sh" --dry-run >/dev/null
[[ ! -e "$MARKER" ]] || fail "Web dry-run invoked privileged persistent mutation"

# Shared lock: a CLI mutation must fail busy when another process owns the same
# core lock. Test against the removal implementation in isolated test mode.
LOCKDIR="$TMP/shared-lock"
mkdir -p "$LOCKDIR"
: > "$LOCKDIR/mutation.lock"
(
  exec 9<>"$LOCKDIR/mutation.lock"
  flock -x 9
  printf ready > "$TMP/lock-ready"
  sleep 5
) &
HOLDER=$!
for _ in {1..50}; do [[ -f "$TMP/lock-ready" ]] && break; sleep 0.02; done
[[ -f "$TMP/lock-ready" ]] || fail "lock holder did not start"

if (
  export GRT_TEST_MODE=1
  export GRT_TEST_LOCK_DIR="$LOCKDIR"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  acquire_mutation_lock
) >/dev/null 2>&1; then
  kill "$HOLDER" 2>/dev/null || true
  wait "$HOLDER" 2>/dev/null || true
  fail "CLI mutation ignored an already-held shared lock"
fi
kill "$HOLDER" 2>/dev/null || true
wait "$HOLDER" 2>/dev/null || true

echo "PASS: Web Management shell contract tests"
