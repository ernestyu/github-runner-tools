#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_eq() { [[ "$1" == "$2" ]] || fail "expected '$2', got '$1'"; }

assert_eq "$(sanitize_component 'My_Project')" "my_project"
assert_eq "$(make_local_id 'ExampleOrg' 'Project-A')" "exampleorg--project-a"
A="$(make_local_id 'example' 'project-a')"
B="$(make_local_id 'another' 'project-a')"
[[ "$A" != "$B" ]] || fail "owner collision"
LONG="$(make_local_id 'VeryLongOrganizationNameWithManyCharacters0123456789' 'VeryLongRepositoryNameWithManyCharacters0123456789')"
(( ${#LONG} <= 64 )) || fail "long local identity exceeds 64 chars"
[[ "$LONG" =~ --[0-9a-f]{8}$ ]] || fail "long local identity lacks hash suffix: $LONG"
assert_eq "$(normalize_runner_version '2.328.0')" "2.328.0"
assert_eq "$(normalize_runner_version 'v2.328.0')" "2.328.0"
if normalize_runner_version 'v2.328' >/dev/null 2>&1; then fail "invalid version accepted"; fi

echo "PASS: pure helper tests"
