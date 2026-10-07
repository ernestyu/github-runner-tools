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
    LoadState) printf '%s\n' "${MOCK_LOAD_STATE-loaded}" ;;
    ActiveState) printf '%s\n' "${MOCK_ACTIVE_STATE-active}" ;;
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

# CLI/Web identity authority must derive the same local id.
SHELL_LOCAL_ID="$(
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  make_local_id "ErnestYu" "HyperGrid"
)"
PY_LOCAL_ID="$(PYTHONPATH="$ROOT/web" python3 -c 'from grt_web_common import make_local_id; print(make_local_id("ErnestYu","HyperGrid"))')"
[[ "$SHELL_LOCAL_ID" == "$PY_LOCAL_ID" ]] || fail "CLI/Web local identity parity mismatch"

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

# Internal Web lifecycle context must be bound to a root Unix peer, not merely
# to caller-controlled environment flags plus a forgeable JSON context reply.
python3 - "$ROOT" <<'PY' || fail "fake same-UID Web registration context was accepted"
import os
import socket
import subprocess
import sys

root = sys.argv[1]
left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    script = f'''
set -Eeuo pipefail
RUNNER_TOOLS_LIB_ONLY=1 source "{root}/scripts/register-runner.sh"
WEB_MODE=1
LOCK_ALREADY_HELD=1
PRIVILEGED_FD={left.fileno()}
acquire_mutation_lock
'''
    proc = subprocess.run(
        ["bash", "-c", script],
        pass_fds=(left.fileno(),),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if os.geteuid() != 0 and proc.returncode == 0:
        raise SystemExit(1)
finally:
    left.close()
    right.close()
PY

python3 - "$ROOT" <<'PY' || fail "fake same-UID Web removal context was accepted"
import os
import socket
import subprocess
import sys

root = sys.argv[1]
left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    script = f'''
set -Eeuo pipefail
RUNNER_TOOLS_LIB_ONLY=1 source "{root}/scripts/remove-runner.sh"
WEB_MODE=1
LOCK_ALREADY_HELD=1
PRIVILEGED_FD={left.fileno()}
acquire_mutation_lock
'''
    proc = subprocess.run(
        ["bash", "-c", script],
        pass_fds=(left.fileno(),),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if os.geteuid() != 0 and proc.returncode == 0:
        raise SystemExit(1)
finally:
    left.close()
    right.close()
PY

# Web setup apply contract must install fixed identities/permissions and remain
# explicit opt-in. These are contract assertions over the installer itself;
# live ownership is separately covered by frozen Debian acceptance.
grep -Fq 'sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$WEB_USER"' "$ROOT/scripts/setup-web-management.sh" ||
  fail "Web apply does not create locked grt-web account"
grep -Fq 'sudo install -o root -g "$WEB_USER" -m 0640 "$TMP_CONFIG" "$CONFIG_FILE"' "$ROOT/scripts/setup-web-management.sh" ||
  fail "Web config ownership contract missing"
grep -Fq 'sudo install -o root -g "$WEB_USER" -m 0640 "$TMP_AUTH" "$AUTH_FILE"' "$ROOT/scripts/setup-web-management.sh" ||
  fail "Web auth ownership contract missing"
grep -Fq 'User=$WEB_USER' "$ROOT/scripts/setup-web-management.sh" ||
  fail "Web service user contract missing"
grep -Fq 'User=root' "$ROOT/scripts/setup-web-management.sh" ||
  fail "dispatcher root service contract missing"
if grep -Eq '^[[:space:]]*(sudo[[:space:]]+)?tailscale[[:space:]]+funnel([[:space:]]|$)' "$ROOT/scripts/setup-web-management.sh"; then
  fail "Web setup contains forbidden executable Tailscale Funnel command"
fi

# Lock/revalidation ordering must remain explicit: CLI acquisition occurs
# before target filesystem validation/mutation in both mutating scripts.
python3 - "$ROOT" <<'PY' || fail "CLI lock/revalidation ordering contract failed"
from pathlib import Path
import sys

root = Path(sys.argv[1])
reg = (root / "scripts/register-runner.sh").read_text()
rem = (root / "scripts/remove-runner.sh").read_text()
for name, text, target in [
    ("register", reg, 'RUNNER_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"'),
    ("remove", rem, 'NEW_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"'),
]:
    lock = text.find("acquire_mutation_lock", text.find("main()"))
    validation = text.find(target, text.find("main()"))
    if lock < 0 or validation < 0 or lock >= validation:
        raise SystemExit(f"{name} does not lock before target validation")
PY

# Authoritative removal/recovery state is revalidated after lock acquisition.
# A stale UI decision must therefore fail before any destructive step.
STALE_BASE="$TMP/stale-runners"
STALE_LOCK="$TMP/stale-lock"
mkdir -p "$STALE_BASE" "$STALE_LOCK"
STALE_DIR="$STALE_BASE/actions-runner-owner--repo"
mkdir -p "$STALE_DIR"
printf '%s\n' 'actions.runner.owner-repo.test-runner.service' > "$STALE_DIR/.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STALE_DIR/svc.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STALE_DIR/config.sh"
chmod +x "$STALE_DIR/svc.sh" "$STALE_DIR/config.sh"

# Stale can_remove=true: configured metadata disappeared before mutation.
if (
  export GRT_TEST_MODE=1
  export GRT_TEST_LOCK_DIR="$STALE_LOCK"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  main --base-dir "$STALE_BASE" owner/repo
) >/dev/null 2>&1; then
  fail "stale normal-removal eligibility was not rejected"
fi
[[ -d "$STALE_DIR" ]] || fail "stale normal-removal rejection mutated runner directory"

# Stale can_recover_local=true: .runner appeared before mutation.
printf '%s\n' '{"gitHubUrl":"https://github.com/owner/repo"}' > "$STALE_DIR/.runner"
if (
  export GRT_TEST_MODE=1
  export GRT_TEST_LOCK_DIR="$STALE_LOCK"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  main --base-dir "$STALE_BASE" --recover-local owner/repo
) >/dev/null 2>&1; then
  fail "stale recovery eligibility was not rejected"
fi
[[ -d "$STALE_DIR" && -f "$STALE_DIR/.runner" ]] ||
  fail "stale recovery rejection mutated runner state"

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
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  acquire_mutation_lock
) >/dev/null 2>&1; then
  kill "$HOLDER" 2>/dev/null || true
  wait "$HOLDER" 2>/dev/null || true
  fail "register mutation ignored an already-held shared lock"
fi

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
