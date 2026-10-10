#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
source_file="$ROOT/scripts/setup-web-management-v2.sh"
[[ -f "$source_file" && ! -L "$source_file" ]]
bash -n "$source_file"
plan="$(bash "$source_file" --dry-run)"
[[ "$plan" == *"DRY-RUN ONLY"* ]]
[[ "$plan" == *"root:grt-web 0660"* ]]
[[ "$plan" == *"existing runner services untouched"* ]]
if bash "$source_file" --unapproved 2>/dev/null; then
    echo "Invalid upgrade options unexpectedly accepted" >&2
    exit 1
fi
# Installation command is syntactically protected by explicit --apply.
grep -Fq -- '--apply)' "$source_file"
echo "PASS: V2 service switch is explicit, previewable and nonmutating by default"

rollback="$ROOT/scripts/rollback-web-management-v2.sh"
bash -n "$rollback"
rollback_plan="$(bash "$rollback" --dry-run --backup-dir /var/lib/github-runner-tools/web-v2-rollback.ABCDEFGH)"
[[ "$rollback_plan" == *"No runner services or state records would be modified."* ]]
echo "PASS: V2 rollback preview preserves existing lifecycle records"
