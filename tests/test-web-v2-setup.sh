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

# Stopping V1 after V2 socket is created may remove the shared systemd
# RuntimeDirectory; assert that the old dispatcher is stopped first.
old_stop=$(grep -n "sudo systemctl disable --now github-runner-tools-dispatch.service" "$source_file" | head -1 | cut -d: -f1)
new_start=$(grep -n "sudo systemctl enable --now github-runner-tools-executor.service" "$source_file" | head -1 | cut -d: -f1)
[[ -n "$old_stop" && -n "$new_start" && "$old_stop" -lt "$new_start" ]]
echo "PASS: old runtime owner is stopped before new Executor binds its socket"

# A V1 writer must be excluded and V1 stopped *before* copying root state.
cutover_lock_line=$(grep -nF 'flock -n "$CUTOVER_LOCK"' "$source_file" | head -1 | cut -d: -f1)
state_backup_line=$(grep -nF 'BACKUP="$(sudo mktemp' "$source_file" | head -1 | cut -d: -f1)
web_stop_line=$(grep -nF 'sudo systemctl stop github-runner-tools-web.service' "$source_file" | head -1 | cut -d: -f1)
[[ -n "$cutover_lock_line" && -n "$state_backup_line" && -n "$web_stop_line" ]]
[[ "$cutover_lock_line" -lt "$web_stop_line" && "$web_stop_line" -lt "$state_backup_line" ]]
grep -Fq 'lifecycle-sha256.txt' "$source_file"
grep -Fq 'lifecycle-metadata.txt' "$source_file"
echo "PASS: cutover lock and quiescent V1 snapshot precede V2 code switch"

preflight_line=$(grep -nF 'unreadable V1 lifecycle schema' "$source_file" | head -1 | cut -d: -f1)
install_code_line=$(grep -nF 'for file in v2_executor_contract.py' "$source_file" | head -1 | cut -d: -f1)
[[ -n "$preflight_line" && -n "$install_code_line" && "$preflight_line" -lt "$install_code_line" ]]
echo "PASS: V1 state schema validation precedes V2 executable replacement"
