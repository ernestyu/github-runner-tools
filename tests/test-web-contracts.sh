#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
fail() { echo "FAIL: $*" >&2; exit 1; }

TMP="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT

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
    LoadState) printf '%s\n' "${MOCK_LOAD_STATE-loaded}" ;;
    ActiveState) printf '%s\n' "${MOCK_ACTIVE_STATE-active}" ;;
    *) printf '%s\n' "" ;;
  esac
  exit 0
fi
exit 0
MOCK
chmod +x "$MOCKBIN/systemctl"

# Status JSON remains the shared machine-readable inventory contract.
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

MOCK_ACTIVE_STATE=inactive JSON_OUT="$(PATH="$MOCKBIN:$PATH" MOCK_ACTIVE_STATE=inactive RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].service_state == "inactive" and .[0].can_recover_local == true' <<<"$JSON_OUT" >/dev/null ||
  fail "inactive service status contract mismatch"

MOCK_LOAD_STATE=not-found JSON_OUT="$(PATH="$MOCKBIN:$PATH" MOCK_LOAD_STATE=not-found RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].service_state == "absent" and .[0].can_recover_local == true' <<<"$JSON_OUT" >/dev/null ||
  fail "absent service status contract mismatch"

MOCK_LOAD_STATE="" JSON_OUT="$(PATH="$MOCKBIN:$PATH" MOCK_LOAD_STATE="" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].service_state == "unknown" and .[0].can_recover_local == false' <<<"$JSON_OUT" >/dev/null ||
  fail "unknown service status contract mismatch"

rm -rf -- "$RECOVERABLE"
INCOMPLETE="$BASE/actions-runner-incomplete"
mkdir -p "$INCOMPLETE"
printf '%s\n' partial > "$INCOMPLETE/partial.txt"
JSON_OUT="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].repository == null and .[0].management_state == "incomplete" and .[0].can_remove == false and .[0].can_recover_local == false' <<<"$JSON_OUT" >/dev/null ||
  fail "incomplete status JSON contract mismatch"

rm -rf -- "$INCOMPLETE"
AMBIG="$BASE/actions-runner-ambiguous"
mkdir -p "$AMBIG"
ln -s nowhere "$AMBIG/.runner"
JSON_OUT="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].repository == null and .[0].management_state == "ambiguous" and .[0].can_remove == false and .[0].can_recover_local == false' <<<"$JSON_OUT" >/dev/null ||
  fail "ambiguous status JSON contract mismatch"
rm -rf -- "$AMBIG"

# CLI and Web derive the same local identity.
SHELL_LOCAL_ID="$(
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  make_local_id "ErnestYu" "HyperGrid"
)"
PY_LOCAL_ID="$(PYTHONPATH="$ROOT/web" python3 -c 'from grt_web_common import make_local_id; print(make_local_id("ErnestYu","HyperGrid"))')"
[[ "$SHELL_LOCAL_ID" == "$PY_LOCAL_ID" ]] || fail "CLI/Web local identity parity mismatch"

# Current active runtime contains no historical Web generation or prerequisite.
for path in   "$ROOT/scripts/setup-web-management.sh"   "$ROOT/scripts/setup-web-management-v2.sh"   "$ROOT/web/dispatcher.py"   "$ROOT/web/lifecycle_worker.py"; do
  [[ ! -e "$path" ]] || fail "historical runtime artifact remains: $path"
done
if grep -REq 'managed-by=github-runner-tools-web-v1|web-dispatch\.sock|tailscaled\.service|setup-web-management'     "$ROOT/scripts" "$ROOT/web" "$ROOT/README.md" "$ROOT/README.zh-CN.md"; then
  fail "active product surface still contains historical Web installation behavior"
fi

# Fresh installer carries the current root service boundary and no sudoers edits.
INSTALLER="$ROOT/scripts/fresh-install.sh"
grep -Fq 'Description=github-runner-tools Executor' "$INSTALLER" ||
  fail "Executor unit template missing"
grep -Fq 'AmbientCapabilities=CAP_SETUID CAP_SETGID' "$INSTALLER" ||
  fail "Executor identity capabilities missing"
grep -Fq 'NoNewPrivileges=true' "$INSTALLER" ||
  fail "Executor/Web NoNewPrivileges missing"
grep -Fq 'RestrictSUIDSGID=true' "$INSTALLER" ||
  fail "Executor/Web RestrictSUIDSGID missing"
grep -Fq 'Requires=github-runner-tools-executor.service' "$INSTALLER" ||
  fail "Web does not require Executor"
if grep -Eq 'NOPASSWD|/etc/sudoers|sudoers\.d|github-runner-tools-dispatch\.service|tailscale' "$INSTALLER"; then
  fail "fresh installer contains forbidden privilege/legacy dependency"
fi

# Current Executor Remove consumes the one-shot token FD through the PTY
# adapter; only interactive CLI retains the official --token argument.
python3 - "$ROOT" <<'PY' || fail "Executor token consumer contract mismatch"
from pathlib import Path
import sys
root = Path(sys.argv[1])
rem = (root / "scripts/remove-runner.sh").read_text()
start = rem.index('if [[ "$WEB_MODE" == "1" ]]; then\n  REMOVE_STAGE="config_remove_failed"')
end = rem.index('\nelse\n  ./config.sh remove --token "$TOKEN"', start)
executor = rem[start:end]
if '--token-fd "$TOKEN_FD" --mode remove -- ./config.sh remove' not in executor:
    raise SystemExit(1)
if './config.sh remove --token "$TOKEN"' in executor:
    raise SystemExit(1)
if 'exec 4>&-' not in executor or 'exec {TOKEN_FD}<&-' not in executor:
    raise SystemExit(1)
PY

# Real identity-drop helper must clear inherited capability state before the
# root->Runner transition. Dedicated UID-drop E2E covers the complete launcher.
python3 - "$ROOT" <<'PY' || fail "current identity-drop helper contract mismatch"
from pathlib import Path
import sys
text = (Path(sys.argv[1]) / "web" / "identity_drop.py").read_text()
for required in ("PR_CAP_AMBIENT_CLEAR_ALL", "PR_SET_KEEPCAPS", "CapInh", "CapAmb"):
    if required not in text:
        raise SystemExit(required)
PY

# Mutation lock acquisition must precede target filesystem derivation.
python3 - "$ROOT" <<'PY' || fail "CLI lock/revalidation ordering contract failed"
from pathlib import Path
import sys
root = Path(sys.argv[1])
for name, target in [
    ("register-runner.sh", 'RUNNER_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"'),
    ("remove-runner.sh", 'NEW_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"'),
]:
    text = (root / "scripts" / name).read_text()
    main = text.find("main()")
    lock = text.find("acquire_mutation_lock", main)
    validation = text.find(target, main)
    if lock < 0 or validation < 0 or lock >= validation:
        raise SystemExit(name)
PY

# CLI/Web parity: mismatched service scope cannot become recoverable identity.
PARITY_BASE="$TMP/parity-runners"
mkdir -p "$PARITY_BASE/actions-runner-owner--repo"
printf '%s\n' 'actions.runner.other-repo.custom.service' > "$PARITY_BASE/actions-runner-owner--repo/.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$PARITY_BASE/actions-runner-owner--repo/svc.sh"
chmod +x "$PARITY_BASE/actions-runner-owner--repo/svc.sh"
PARITY_JSON="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$PARITY_BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].can_recover_local == false and (.[0].management_state == "ambiguous" or .[0].repository == null)' <<<"$PARITY_JSON" >/dev/null ||
  fail "Status accepted mismatched recovery service identity"
if (
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  validate_recovery_identity     "$PARITY_BASE/actions-runner-owner--repo"     "$PARITY_BASE/actions-runner-owner--repo" owner repo
) >/dev/null 2>&1; then
  fail "CLI recovery authority accepted mismatched service identity"
fi

# Stale UI eligibility is revalidated after the shared lock.
STALE_BASE="$TMP/stale-runners"
STALE_LOCK="$TMP/stale-lock"
mkdir -p "$STALE_BASE" "$STALE_LOCK"
STALE_DIR="$STALE_BASE/actions-runner-owner--repo"
mkdir -p "$STALE_DIR"
printf '%s\n' 'actions.runner.owner-repo.test-runner.service' > "$STALE_DIR/.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STALE_DIR/svc.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STALE_DIR/config.sh"
chmod +x "$STALE_DIR/svc.sh" "$STALE_DIR/config.sh"

if (
  export GRT_TEST_MODE=1 GRT_TEST_LOCK_DIR="$STALE_LOCK"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  main --base-dir "$STALE_BASE" owner/repo
) >/dev/null 2>&1; then
  fail "stale normal-removal eligibility was not rejected"
fi
[[ -d "$STALE_DIR" ]] || fail "stale normal-removal rejection mutated runner directory"

printf '%s\n' '{"gitHubUrl":"https://github.com/owner/repo"}' > "$STALE_DIR/.runner"
if (
  export GRT_TEST_MODE=1 GRT_TEST_LOCK_DIR="$STALE_LOCK"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  main --base-dir "$STALE_BASE" --recover-local owner/repo
) >/dev/null 2>&1; then
  fail "stale recovery eligibility was not rejected"
fi
[[ -d "$STALE_DIR" && -f "$STALE_DIR/.runner" ]] ||
  fail "stale recovery rejection mutated runner state"

# Core CLI lock bootstrap remains independent from Web installation.
CORE_LOCK_ROOT="$TMP/core-lock-bootstrap"
(
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  MUTATION_LOCK_DIR="$CORE_LOCK_ROOT"
  MUTATION_LOCK_FILE="$CORE_LOCK_ROOT/mutation.lock"
  ensure_mutation_lock
)
[[ "$(stat -c '%U:%G:%a:%F' "$CORE_LOCK_ROOT")" == "root:root:755:directory" ]] ||
  fail "core lock directory bootstrap contract mismatch"
[[ "$(stat -c '%U:%G:%a' "$CORE_LOCK_ROOT/mutation.lock")" == "root:$(id -gn):660" ]] ||
  fail "core lock file bootstrap contract mismatch"
sudo rm -rf -- "$CORE_LOCK_ROOT"

ln -s "$TMP" "$CORE_LOCK_ROOT"
if (
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  MUTATION_LOCK_DIR="$CORE_LOCK_ROOT"
  MUTATION_LOCK_FILE="$CORE_LOCK_ROOT/mutation.lock"
  ensure_mutation_lock
) >/dev/null 2>&1; then
  fail "symlinked core lock directory was accepted"
fi
rm -f -- "$CORE_LOCK_ROOT"

sudo install -d -o root -g root -m 0777 "$CORE_LOCK_ROOT"
if (
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  MUTATION_LOCK_DIR="$CORE_LOCK_ROOT"
  MUTATION_LOCK_FILE="$CORE_LOCK_ROOT/mutation.lock"
  ensure_mutation_lock
) >/dev/null 2>&1; then
  fail "wrong-mode core lock directory was accepted"
fi
sudo rm -rf -- "$CORE_LOCK_ROOT"

# A concurrently-held common mutation lock rejects both CLI mutation paths.
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

for script in register-runner.sh remove-runner.sh; do
  if (
    export GRT_TEST_MODE=1 GRT_TEST_LOCK_DIR="$LOCKDIR"
    RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/$script"
    acquire_mutation_lock
  ) >/dev/null 2>&1; then
    kill "$HOLDER" 2>/dev/null || true
    wait "$HOLDER" 2>/dev/null || true
    fail "$script ignored an already-held shared lock"
  fi
done
kill "$HOLDER" 2>/dev/null || true
wait "$HOLDER" 2>/dev/null || true

echo "PASS: current Web/CLI contract tests"
