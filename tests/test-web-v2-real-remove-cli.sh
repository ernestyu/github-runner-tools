#!/usr/bin/env bash
set -Eeuo pipefail
# Run the actual remove-runner.sh main() with a fake GitHub CLI, fake
# systemd, and temporary local runner directory. Never touch host runners.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/runner-home"
cp "$ROOT/web/pty_token_adapter.py" "$TMP/pty_token_adapter.py"
chmod 0755 "$TMP/pty_token_adapter.py"
export RUNNER_BASE_DIR="$TMP/runner-home" RUNNER_TOOLS_LIB_ONLY=1
export GRT_EXECUTOR_CONTEXT=1 GRT_PTY_ADAPTER="$TMP/pty_token_adapter.py"
export GRT_FAKE_REMOVE_ATTEMPTS="$TMP/attempts"
source "$ROOT/scripts/remove-runner.sh"
STATE="$TMP/stages"; SERVICE="$TMP/service-state"; OPS="$TMP/service-ops"
: > "$STATE"; : > "$OPS"; : > "$GRT_FAKE_REMOVE_ATTEMPTS"
web_context_check() { :; }
acquire_mutation_lock() { :; }
web_remove_state() {
  case "$1" in
    BEGIN)
      if grep -qx BEGIN "$STATE"; then return 95; fi
      ;;
    CONFIRM_REMOTE_REMOVED)
      WEB_PRIV_RESPONSE='{"ok":true,"device":1,"inode":2}' ;;
    COMPLETE) : ;;
    *) return 93 ;;
  esac
  printf '%s\n' "$1" >> "$STATE"
}
web_service_state() { cat "$SERVICE"; }
web_service_operation() {
  printf '%s\n' "$1" >> "$OPS"
  case "$1" in
    service_stop) : ;;
    service_uninstall) printf '%s\n' absent > "$SERVICE" ;;
    *) return 91 ;;
  esac
}
python3() {
  if [[ "${1:-}" == *"/web-service-record.py" ]]; then
    if [[ "${2:-}" == quarantine ]]; then printf '%s' fixture-proof; fi
    return 0
  fi
  if [[ "${1:-}" == *"/web-runner-cleanup.py" ]]; then
    [[ "$3" == "$RUNNER_DIR" ]] || return 94
    command rm -rf -- "$RUNNER_DIR"
    return 0
  fi
  command python3 "$@"
}
prepare_runner() {
  local dir="$TMP/runner-home/actions-runner-owner--repo"
  mkdir -p "$dir"
  printf '%s\n' '{"agentName":"runner","agentId":7,"gitHubUrl":"https://github.com/owner/repo"}' > "$dir/.runner"
  printf '%s\n' 'actions.runner.owner-repo.runner.service' > "$dir/.service"
  cat > "$dir/config.sh" <<'PY'
#!/usr/bin/env python3
import os, sys
if sys.argv[1:] != ["remove"]:
    sys.exit(70)
if any("fixture-only-token" in arg for arg in sys.argv):
    sys.exit(71)
with open(os.environ["GRT_FAKE_REMOVE_ATTEMPTS"], "a") as f:
    f.write("called\n")
sys.stdout.write("Please enter the removal token: ")
sys.stdout.flush()
if sys.stdin.readline().rstrip("\r\n") != "fixture-only-token":
    sys.exit(72)
if os.environ.get("GRT_FAKE_REMOVE_FAIL") == "1":
    sys.exit(73)
PY
  chmod 0755 "$dir/config.sh"
  printf '#!/bin/sh\nexit 0\n' > "$dir/svc.sh"
  chmod 0755 "$dir/svc.sh"
  printf '%s\n' active > "$SERVICE"
  : > "$STATE"; : > "$OPS"; : > "$GRT_FAKE_REMOVE_ATTEMPTS"
}
invoke_remove() {
  exec 3<<<"fixture-only-token"
  exec 4< /dev/null
  ( main --executor-mode --token-fd 3 --privileged-fd 4 --lock-already-held owner/repo )
}
prepare_runner
invoke_remove
[[ ! -e "$TMP/runner-home/actions-runner-owner--repo" ]]
[[ "$(cat "$STATE")" == "$(printf '%s\n' BEGIN CONFIRM_REMOTE_REMOVED COMPLETE)" ]]
[[ "$(cat "$OPS")" == "$(printf '%s\n' service_stop service_uninstall)" ]]
[[ "$(wc -l < "$GRT_FAKE_REMOVE_ATTEMPTS")" -eq 1 ]]
echo "PASS: full Remove CLI with official PTY fixture and fake systemd"
prepare_runner
export GRT_FAKE_REMOVE_FAIL=1
if invoke_remove; then
  echo "Uncertain GitHub removal unexpectedly succeeded" >&2; exit 81
fi
[[ -d "$TMP/runner-home/actions-runner-owner--repo" ]]
[[ "$(cat "$STATE")" == BEGIN ]]
[[ "$(wc -l < "$GRT_FAKE_REMOVE_ATTEMPTS")" -eq 1 ]]
# Same target/instance cannot retry after BEGIN UNKNOWN. No second attempt.
if invoke_remove; then
  echo "Duplicate UNKNOWN removal unexpectedly succeeded" >&2; exit 82
fi
[[ "$(wc -l < "$GRT_FAKE_REMOVE_ATTEMPTS")" -eq 1 ]]
[[ -d "$TMP/runner-home/actions-runner-owner--repo" ]]
echo "PASS: failed remote outcome keeps Runner and refuses duplicate Remove"
