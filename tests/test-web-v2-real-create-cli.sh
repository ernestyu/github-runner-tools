#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/runner-home"
REPO="owner/repo"; STAGES="$TMP/stages"; PRIV="$TMP/privileged"
: > "$STAGES"; : > "$PRIV"
export RUNNER_BASE_DIR="$TMP/runner-home" GRT_EXECUTOR_CONTEXT=1
export GRT_TEST_MODE=1 GRT_TEST_LOCK_DIR="$TMP/lock"
export GRT_PTY_ADAPTER="$ROOT/web/pty_token_adapter.py"
export RUNNER_TOOLS_LIB_ONLY=1
source "$ROOT/scripts/register-runner.sh"
web_context_check() { :; }
validate_local_archive_platform() { ARCHIVE_ROOT="$TMP"; MIN_FREE_PERCENT=1; }
configure_runner_archive_hook() { :; }
acquire_mutation_lock() { :; }
ps() {
  if [[ "${1:-}" == "-p" && "${2:-}" == "1" ]]; then echo systemd; else command ps "$@"; fi
}
curl() {
  local out="" last=""
  while (($#)); do
    if [[ "$1" == "-o" ]]; then out="$2"; shift 2; else last="$1"; shift; fi
  done
  if [[ "$last" == *"/releases/latest" ]]; then
    printf '%s\n' '{"tag_name":"v2.328.0","assets":[{"name":"actions-runner-linux-x64-2.328.0.tar.gz","browser_download_url":"https://example.invalid/fixture","digest":""}]}' > "$out"
  else
    : > "$out"
  fi
}
tar() {
  [[ "$1" == "xzf" ]] || return 91
  mkdir -p bin
  printf '#!/bin/sh\nexit 0\n' > bin/Runner.Listener
  chmod 0755 bin/Runner.Listener
  cat > config.sh <<'PY'
#!/usr/bin/env python3
import json, sys
if any("fixture-only-token" in arg for arg in sys.argv):
    sys.exit(97)
sys.stdout.write("Enter your token: ")
sys.stdout.flush()
if sys.stdin.readline().rstrip("\r\n") != "fixture-only-token":
    sys.exit(98)
name = sys.argv[sys.argv.index("--name") + 1]
url = sys.argv[sys.argv.index("--url") + 1]
with open(".runner", "w") as f:
    json.dump({"agentName": name, "agentId": 7, "gitHubUrl": url}, f)
PY
  chmod 0755 config.sh
  printf '#!/bin/sh\nexit 0\n' > runsvc.sh
  chmod 0755 runsvc.sh
}
python3() {
  if [[ "${1:-}" == *"/web-service-record.py" ]]; then return 0; fi
  command python3 "$@"
}
web_create_stage() { printf '%s\n' "$1" >> "$STAGES"; }
web_priv_request() {
  local op
  op="$(jq -r .op <<< "$1")"
  printf '%s\n' "$op" >> "$PRIV"
  case "$op" in
    service_install) WEB_PRIV_RESPONSE='{"ok":true,"service":"actions.runner.owner-repo.local-ci-owner--repo.service"}' ;;
    service_state) WEB_PRIV_RESPONSE='{"ok":true,"state":"active"}' ;;
    create_state|registration_permission_normalize|registration_attestation_create|service_start)
      WEB_PRIV_RESPONSE='{"ok":true}' ;;
    *) echo "unrecognized root operation $op" >&2; return 92 ;;
  esac
}
exec 3<<<"fixture-only-token"
exec 4< /dev/null
main --executor-mode --token-fd 3 --privileged-fd 4 --lock-already-held "$REPO"
EXPECTED="$(printf '%s\n' PRE_REGISTRATION REGISTERED_PERMISSION_INCOMPLETE REGISTERED_UNIT_INCOMPLETE REGISTERED_START_INCOMPLETE REGISTERED_HEALTH_UNKNOWN CREATE_COMPLETE)"
[[ "$(cat "$STAGES")" == "$EXPECTED" ]] || { echo "Create stage mismatch" >&2; exit 93; }
EXPECTED_PRIV="$(printf '%s\n' registration_permission_normalize registration_attestation_create service_install service_start service_state)"
[[ "$(cat "$PRIV")" == "$EXPECTED_PRIV" ]] || { echo "Create privilege path mismatch" >&2; exit 94; }
[[ -f "$TMP/runner-home/actions-runner-owner--repo/.runner" ]]
echo "PASS: actual Create CLI reaches complete with fake GitHub/systemd"
