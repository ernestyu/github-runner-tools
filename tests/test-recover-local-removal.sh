#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fail() { echo "FAIL: $*" >&2; exit 1; }

TMP="$(mktemp -d)"
export GRT_TEST_MODE=1
export GRT_TEST_LOCK_DIR="$TMP/mutation-lock"
cleanup_test() {
  chmod -R u+rwX "$TMP" 2>/dev/null || true
  rm -rf -- "$TMP"
}
trap cleanup_test EXIT

mkdir -p "$TMP/mockbin"
cat > "$TMP/mockbin/systemctl" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "show" ]]; then
  if [[ -n "${MOCK_STATE_FILE:-}" && -f "$MOCK_STATE_FILE" ]]; then
    cat "$MOCK_STATE_FILE"
  else
    printf '%s\n' "${MOCK_LOAD_STATE:-not-found}"
  fi
  exit 0
fi
exit 0
MOCK

cat > "$TMP/mockbin/sudo" <<'MOCK'
#!/usr/bin/env bash
exec "$@"
MOCK
chmod +x "$TMP/mockbin/systemctl" "$TMP/mockbin/sudo"

export PATH="$TMP/mockbin:$PATH"

RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
trap cleanup_test EXIT

# CLI surface exists.
grep -q -- '--recover-local' "$ROOT/scripts/remove-runner.sh" ||
  fail "--recover-local CLI option missing"

# Service scope validation: custom runner names are allowed when the complete
# repository scope is present.
service_name_matches_repo_scope   "actions.runner.ernestyu-example.my-custom-runner.service"   "ernestyu" "example" ||
  fail "valid custom runner name was rejected"

# Repository scope matching is ASCII case-insensitive.
service_name_matches_repo_scope   "actions.runner.ErNeStYu-ExAmPlE.Custom.Service"   "ernestyu" "example" ||
  fail "case-insensitive repository scope was rejected"

# Wrong scope, unanchored substring, empty runner name, and official-style
# truncated repository scope must all fail closed.
if service_name_matches_repo_scope   "actions.runner.someoneelse-example.custom.service"   "ernestyu" "example"; then
  fail "mismatched repository scope was accepted"
fi

if service_name_matches_repo_scope   "foo.actions.runner.ernestyu-example.custom.service"   "ernestyu" "example"; then
  fail "unanchored repository substring was accepted"
fi

if service_name_matches_repo_scope   "actions.runner.ernestyu-example.service"   "ernestyu" "example"; then
  fail "empty runner-name segment was accepted"
fi

if service_name_matches_repo_scope   "actions.runner.ernestyu-very-long-repos.custom-4821.service"   "ernestyu" "very-long-repository-name"; then
  fail "truncated repository scope was accepted"
fi

# Official allowed-character normalization for repository scope.
[[ "$(normalize_service_repo_scope 'Owner.Name' 'Repo_Name-1')" == "Owner.Name-Repo_Name-1" ]] ||
  fail "repository scope normalization changed allowed characters"

make_runner_residue() {
  local dir="$1" service_name="$2" uninstall_mode="${3:-success}"
  mkdir -p "$dir"
  printf '%s\n' "$service_name" > "$dir/.service"
  cat > "$dir/svc.sh" <<'MOCK'
#!/usr/bin/env bash
case "${1:-}" in
  stop)
    exit "${MOCK_STOP_RC:-0}"
    ;;
  uninstall)
    case "${MOCK_UNINSTALL_MODE:-success}" in
      success)
        printf '%s\n' not-found > "$MOCK_STATE_FILE"
        exit 0
        ;;
      nonzero_absent)
        printf '%s\n' not-found > "$MOCK_STATE_FILE"
        exit 1
        ;;
      fail_present)
        printf '%s\n' loaded > "$MOCK_STATE_FILE"
        exit 1
        ;;
      *)
        exit 2
        ;;
    esac
    ;;
esac
exit 0
MOCK
  chmod +x "$dir/svc.sh"
  export MOCK_UNINSTALL_MODE="$uninstall_mode"
}

assert_identity_rejected() {
  local runner_dir="$1" expected_dir="$2" label="$3"
  if (
    validate_recovery_identity "$runner_dir" "$expected_dir" "ernestyu" "example"
  ) >/dev/null 2>&1; then
    fail "$label was accepted"
  fi
}

# Any existing .runner object must fail before service mutation.
IDENTITY="$TMP/identity/actions-runner-ernestyu--example"
mkdir -p "$IDENTITY"
printf '%s\n' 'actions.runner.ernestyu-example.custom.service' > "$IDENTITY/.service"
cat > "$IDENTITY/svc.sh" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
chmod +x "$IDENTITY/svc.sh"

printf '%s\n' '{"gitHubUrl":"https://github.com/ernestyu/example"}' > "$IDENTITY/.runner"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "configured .runner"

printf '%s\n' '{malformed' > "$IDENTITY/.runner"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "malformed .runner"

: > "$IDENTITY/.runner"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "empty .runner"

printf '%s\n' 'unreadable' > "$IDENTITY/.runner"
chmod 000 "$IDENTITY/.runner"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "unreadable .runner"
chmod 600 "$IDENTITY/.runner"

rm -f -- "$IDENTITY/.runner"
ln -s does-not-exist "$IDENTITY/.runner"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "broken-symlink .runner"
rm -f -- "$IDENTITY/.runner"

# With .runner absent, valid new-style identity may continue.
[[ "$(validate_recovery_identity "$IDENTITY" "$IDENTITY" "ernestyu" "example")" ==    "actions.runner.ernestyu-example.custom.service" ]] ||
  fail "valid .runner-absent recovery identity was rejected"

# Strict .service requirements.
rm -f -- "$IDENTITY/.service"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "missing .service"

: > "$IDENTITY/.service"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "empty .service"

printf '%s\n' 'actions.runner.ernestyu-example.custom.service' > "$IDENTITY/.service"
chmod 000 "$IDENTITY/.service"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "unreadable .service"
chmod 600 "$IDENTITY/.service"

printf '%s\n%s\n'   'actions.runner.ernestyu-example.custom.service'   'actions.runner.ernestyu-example.other.service' > "$IDENTITY/.service"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "multi-line .service"

rm -f -- "$IDENTITY/.service"
printf '%s\n' 'actions.runner.ernestyu-example.custom.service' > "$TMP/service-target"
ln -s "$TMP/service-target" "$IDENTITY/.service"
assert_identity_rejected "$IDENTITY" "$IDENTITY" "symlink .service"
rm -f -- "$IDENTITY/.service"

# Main recovery flow: present service -> stop/uninstall -> confirmed absent ->
# delete only local runner directory. No config.sh exists, proving recovery
# does not require or call GitHub unregister.
BASE="$TMP/base"
RUNNER="$BASE/actions-runner-ernestyu--example"
STATE="$TMP/service-state"
ARCHIVE_SENTINEL="$TMP/archive/ernestyu/example/keep.txt"
mkdir -p "$(dirname "$ARCHIVE_SENTINEL")"
printf '%s\n' keep > "$ARCHIVE_SENTINEL"
printf '%s\n' loaded > "$STATE"
make_runner_residue   "$RUNNER"   "actions.runner.ernestyu-example.my-custom-runner.service"   success

(
  export MOCK_STATE_FILE="$STATE"
  export MOCK_UNINSTALL_MODE=success
  read_tty_line() { printf '%s' "RECOVER-REMOVE"; }
  main --base-dir "$BASE" --recover-local ernestyu/example
) >/dev/null || fail "valid local recovery flow failed"

[[ ! -e "$RUNNER" ]] || fail "successful recovery did not delete runner directory"
[[ -f "$ARCHIVE_SENTINEL" ]] || fail "recovery modified archive data"

# Service already absent is a valid recovery state.
RUNNER="$BASE/actions-runner-ernestyu--example"
printf '%s\n' not-found > "$STATE"
make_runner_residue   "$RUNNER"   "actions.runner.ernestyu-example.custom.service"   success
(
  export MOCK_STATE_FILE="$STATE"
  read_tty_line() { printf '%s' "RECOVER-REMOVE"; }
  main --base-dir "$BASE" --recover-local ernestyu/example
) >/dev/null || fail "already-absent service recovery failed"
[[ ! -e "$RUNNER" ]] || fail "already-absent service recovery kept runner directory"

# Non-zero uninstall is allowed only if the post-check proves the service absent.
RUNNER="$BASE/actions-runner-ernestyu--example"
printf '%s\n' loaded > "$STATE"
make_runner_residue   "$RUNNER"   "actions.runner.ernestyu-example.custom.service"   nonzero_absent
(
  export MOCK_STATE_FILE="$STATE"
  export MOCK_UNINSTALL_MODE=nonzero_absent
  read_tty_line() { printf '%s' "RECOVER-REMOVE"; }
  main --base-dir "$BASE" --recover-local ernestyu/example
) >/dev/null 2>&1 || fail "non-zero uninstall with confirmed-absent service failed"
[[ ! -e "$RUNNER" ]] || fail "confirmed-absent retry kept runner directory"

# Genuine uninstall failure keeps the directory.
RUNNER="$BASE/actions-runner-ernestyu--example"
printf '%s\n' loaded > "$STATE"
make_runner_residue   "$RUNNER"   "actions.runner.ernestyu-example.custom.service"   fail_present
if (
  export MOCK_STATE_FILE="$STATE"
  export MOCK_UNINSTALL_MODE=fail_present
  read_tty_line() { printf '%s' "RECOVER-REMOVE"; }
  main --base-dir "$BASE" --recover-local ernestyu/example
) >/dev/null 2>&1; then
  fail "service uninstall failure incorrectly succeeded"
fi
[[ -d "$RUNNER" ]] || fail "service uninstall failure deleted runner directory"
rm -rf -- "$RUNNER"

# Unknown service state keeps the directory.
RUNNER="$BASE/actions-runner-ernestyu--example"
make_runner_residue   "$RUNNER"   "actions.runner.ernestyu-example.custom.service"   success
: > "$STATE"
if (
  export MOCK_STATE_FILE="$STATE"
  read_tty_line() { printf '%s' "RECOVER-REMOVE"; }
  main --base-dir "$BASE" --recover-local ernestyu/example
) >/dev/null 2>&1; then
  fail "unknown service state incorrectly succeeded"
fi
[[ -d "$RUNNER" ]] || fail "unknown service state deleted runner directory"
rm -rf -- "$RUNNER"

# Configured runner guard through the real recovery CLI path.
RUNNER="$BASE/actions-runner-ernestyu--example"
printf '%s\n' loaded > "$STATE"
make_runner_residue   "$RUNNER"   "actions.runner.ernestyu-example.custom.service"   success
printf '%s\n' '{}' > "$RUNNER/.runner"
if (
  export MOCK_STATE_FILE="$STATE"
  read_tty_line() { printf '%s' "RECOVER-REMOVE"; }
  main --base-dir "$BASE" --recover-local ernestyu/example
) >/dev/null 2>&1; then
  fail "configured runner was accepted by --recover-local"
fi
[[ -d "$RUNNER" ]] || fail "configured runner guard deleted runner directory"
[[ "$(cat "$STATE")" == "loaded" ]] || fail "configured runner guard mutated service"
rm -rf -- "$RUNNER"

# Repository-scope mismatch fails before service mutation.
RUNNER="$BASE/actions-runner-ernestyu--example"
printf '%s\n' loaded > "$STATE"
make_runner_residue   "$RUNNER"   "actions.runner.otherowner-example.custom.service"   success
if (
  export MOCK_STATE_FILE="$STATE"
  read_tty_line() { printf '%s' "RECOVER-REMOVE"; }
  main --base-dir "$BASE" --recover-local ernestyu/example
) >/dev/null 2>&1; then
  fail "service repository-scope mismatch was accepted"
fi
[[ -d "$RUNNER" ]] || fail "identity mismatch deleted runner directory"
[[ "$(cat "$STATE")" == "loaded" ]] || fail "identity mismatch mutated service"
rm -rf -- "$RUNNER"

# Official-style truncated scope fails before service mutation.
RUNNER="$BASE/actions-runner-ernestyu--example"
printf '%s\n' loaded > "$STATE"
make_runner_residue   "$RUNNER"   "actions.runner.ernestyu-exam.custom-4821.service"   success
if (
  export MOCK_STATE_FILE="$STATE"
  read_tty_line() { printf '%s' "RECOVER-REMOVE"; }
  main --base-dir "$BASE" --recover-local ernestyu/example
) >/dev/null 2>&1; then
  fail "truncated repository scope was accepted"
fi
[[ -d "$RUNNER" ]] || fail "truncated service identity deleted runner directory"
[[ "$(cat "$STATE")" == "loaded" ]] || fail "truncated service identity mutated service"
rm -rf -- "$RUNNER"

# Legacy repo-only residue without .runner is intentionally unsupported.
LEGACY="$BASE/actions-runner-example"
mkdir -p "$LEGACY"
printf '%s\n' 'actions.runner.ernestyu-example.custom.service' > "$LEGACY/.service"
cat > "$LEGACY/svc.sh" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
chmod +x "$LEGACY/svc.sh"
if (
  read_tty_line() { printf '%s' "RECOVER-REMOVE"; }
  main --base-dir "$BASE" --recover-local ernestyu/example
) >/dev/null 2>&1; then
  fail "ambiguous legacy recovery was accepted"
fi
[[ -d "$LEGACY" ]] || fail "ambiguous legacy recovery deleted directory"

echo "PASS: local runner recovery removal tests"
