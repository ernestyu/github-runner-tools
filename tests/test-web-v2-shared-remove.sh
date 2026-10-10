#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
export RUNNER_TOOLS_LIB_ONLY=1
source "$ROOT/scripts/remove-runner.sh"
TRACE=""
web_remove_state() { TRACE="${TRACE}WEB:$1;"; }
cli_remove_authority() { TRACE="${TRACE}CLI:$1;"; }
WEB_MODE=1
for stage in BEGIN CONFIRM_REMOTE_REMOVED COMPLETE; do remove_lifecycle_stage "$stage"; done
[[ "$TRACE" == "WEB:BEGIN;WEB:CONFIRM_REMOTE_REMOVED;WEB:COMPLETE;" ]] || { echo "Unexpected Web stage sequence" >&2; exit 1; }
TRACE=""
WEB_MODE=0
for stage in BEGIN CONFIRM_REMOTE_REMOVED COMPLETE; do remove_lifecycle_stage "$stage"; done
[[ "$TRACE" == "CLI:BEGIN;CLI:CONFIRM_REMOTE_REMOVED;CLI:COMPLETE;" ]] || { echo "Unexpected CLI stage sequence" >&2; exit 1; }
echo "PASS: terminal and Executor Remove share the same durable lifecycle stage function"
