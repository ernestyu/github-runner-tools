#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$ROOT/hooks/archive-job-completed.sh"
LIB="$ROOT/scripts/lib/archive-common.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT

for cmd in jq rsync timeout; do command -v "$cmd" >/dev/null 2>&1 || { echo "SKIP: $cmd not installed"; exit 0; }; done

ARCHIVE="$TMP/archive"
WORK="$TMP/work"
OUTSIDE="$TMP/outside"
SUMMARY="$TMP/summary.md"
mkdir -p "$ARCHIVE" "$WORK/results" "$WORK/node_modules/pkg" "$WORK/.git" "$OUTSIDE"
printf 'evidence\n' > "$WORK/results/result.jsonl"
printf 'dependency\n' > "$WORK/node_modules/pkg/file.js"
printf 'gitmeta\n' > "$WORK/.git/config"
printf 'secret-outside\n' > "$OUTSIDE/private.txt"
ln -s "$OUTSIDE/private.txt" "$WORK/external-link"
: > "$SUMMARY"

cat > "$TMP/archive.conf" <<EOF
ARCHIVE_ROOT=$ARCHIVE
RETENTION_DAYS=90
MIN_FREE_PERCENT=1
COPY_TIMEOUT_SECONDS=30
EOF

run_hook() {
  env     GRT_TEST_MODE=1     GRT_TEST_LIB="$LIB"     GRT_TEST_CONFIG="$TMP/archive.conf"     GITHUB_ACTIONS=false     GITHUB_REPOSITORY="ErnestYu/TestRepo"     GITHUB_RUN_ID=100     GITHUB_RUN_ATTEMPT=1     GITHUB_JOB=test     GITHUB_SHA=abcdef123456     GITHUB_REF=refs/heads/main     GITHUB_WORKFLOW=CI     GITHUB_WORKSPACE="$WORK"     RUNNER_NAME=local-ci-test     GITHUB_STEP_SUMMARY="$SUMMARY"     bash "$HOOK"
}

run_hook
ATTEMPT="$ARCHIVE/ernestyu/testrepo/100/attempt_1"
mapfile -t manifests < <(find "$ATTEMPT" -name manifest.json -type f)
[[ ${#manifests[@]} -eq 1 ]] || fail "expected one PASS manifest"
JOBDIR="$(dirname "${manifests[0]}")"
[[ -f "$JOBDIR/workspace/results/result.jsonl" ]] || fail "results directory was not archived"
[[ ! -e "$JOBDIR/workspace/node_modules/pkg/file.js" ]] || fail "node_modules was not excluded"
[[ ! -e "$JOBDIR/workspace/.git/config" ]] || fail ".git was not excluded"
[[ ! -f "$JOBDIR/workspace/external-link" ]] || fail "external symlink target was copied as a regular file"
[[ "$(jq -r '.archive_status' "${manifests[0]}")" == "PASS" ]] || fail "manifest status not PASS"
( cd "$JOBDIR" && sha256sum -c manifest.sha256 >/dev/null ) || fail "manifest hash invalid"
grep -q 'Archive status: PASS' "$SUMMARY" || fail "PASS summary not written"

# A second legitimate execution with the same weak identity must not overwrite.
run_hook
mapfile -t manifests < <(find "$ATTEMPT" -name manifest.json -type f)
[[ ${#manifests[@]} -eq 2 ]] || fail "collision did not produce a second independent archive"
keys="$(for m in "${manifests[@]}"; do jq -r '.job_key' "$m"; done | sort -u | wc -l)"
[[ "$keys" -eq 2 ]] || fail "colliding executions reused the same JOB_KEY"

echo "PASS: archive completed-hook integration tests"
