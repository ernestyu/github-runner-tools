#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fail() { echo "FAIL: $*" >&2; exit 1; }
assert_eq() { [[ "$1" == "$2" ]] || fail "expected '$2', got '$1'"; }

TMP="$(mktemp -d)"
cleanup_test() { rm -rf -- "$TMP"; }
trap cleanup_test EXIT

# Relative base directories must canonicalize before later cd operations.
mkdir -p "$TMP/work/runners"
(
  cd "$TMP/work"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  ACTUAL="$(canonicalize_existing_dir ./runners)"
  EXPECTED="$(cd ./runners && pwd -P)"
  assert_eq "$ACTUAL" "$EXPECTED"
  [[ "$ACTUAL" = /* ]] || fail "register base directory did not become absolute"
)

(
  cd "$TMP/work"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  ACTUAL="$(canonicalize_existing_dir ./runners)"
  EXPECTED="$(cd ./runners && pwd -P)"
  assert_eq "$ACTUAL" "$EXPECTED"
  [[ "$ACTUAL" = /* ]] || fail "remove base directory did not become absolute"
)

# Default registration must never silently replace an existing remote runner.
if grep -Eq '(^|[[:space:]\\])--replace([[:space:]\\]|$)' "$ROOT/scripts/register-runner.sh"; then
  fail "register-runner.sh still contains --replace"
fi

# Build command mocks for removal recovery tests.
mkdir -p "$TMP/mockbin" "$TMP/runner"
cat > "$TMP/mockbin/systemctl" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "show" ]]; then
  printf '%s\n' "${MOCK_LOAD_STATE:-loaded}"
  exit 0
fi
exit 0
MOCK
cat > "$TMP/mockbin/sudo" <<'MOCK'
#!/usr/bin/env bash
exec "$@"
MOCK
chmod +x "$TMP/mockbin/systemctl" "$TMP/mockbin/sudo"

cat > "$TMP/runner/svc.sh" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "uninstall" ]]; then
  exit "${MOCK_UNINSTALL_RC:-0}"
fi
exit 0
MOCK
chmod +x "$TMP/runner/svc.sh"

# Retry case: a prior attempt already removed the systemd unit. The second
# attempt must continue instead of getting stuck before GitHub unregister.
(
  export PATH="$TMP/mockbin:$PATH"
  export MOCK_LOAD_STATE=not-found
  export MOCK_UNINSTALL_RC=1
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  cd "$TMP/runner"
  printf '%s\n' 'actions.runner.example.service' > .service
  uninstall_service_safely >/dev/null
) || fail "already-absent systemd service blocked removal retry"

# Genuine uninstall failure must remain fatal when the unit still exists.
if (
  export PATH="$TMP/mockbin:$PATH"
  export MOCK_LOAD_STATE=loaded
  export MOCK_UNINSTALL_RC=1
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  cd "$TMP/runner"
  printf '%s\n' 'actions.runner.example.service' > .service
  uninstall_service_safely >/dev/null 2>&1
); then
  fail "real systemd uninstall failure was incorrectly ignored"
fi

# Normal uninstall success should pass.
(
  export PATH="$TMP/mockbin:$PATH"
  export MOCK_LOAD_STATE=loaded
  export MOCK_UNINSTALL_RC=0
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  cd "$TMP/runner"
  printf '%s\n' 'actions.runner.example.service' > .service
  uninstall_service_safely >/dev/null
) || fail "successful service uninstall was rejected"

# Missing .service is UNKNOWN, not ABSENT. Removal must stop rather than
# assuming that no systemd unit exists.
if (
  export PATH="$TMP/mockbin:$PATH"
  export MOCK_LOAD_STATE=not-found
  export MOCK_UNINSTALL_RC=0
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  cd "$TMP/runner"
  rm -f -- .service
  uninstall_service_safely >/dev/null 2>&1
); then
  fail "missing .service was incorrectly treated as an absent service"
fi

# Empty .service is also UNKNOWN.
if (
  export PATH="$TMP/mockbin:$PATH"
  export MOCK_LOAD_STATE=not-found
  export MOCK_UNINSTALL_RC=0
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  cd "$TMP/runner"
  : > .service
  uninstall_service_safely >/dev/null 2>&1
); then
  fail "empty .service was incorrectly treated as an absent service"
fi

echo "PASS: path and removal recovery tests"
