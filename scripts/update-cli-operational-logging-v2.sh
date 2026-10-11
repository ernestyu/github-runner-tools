#!/usr/bin/env bash
# Controlled update of an ALREADY-INSTALLED Web V2 CLI logger and one
# Executor ReadWritePaths exception. NEVER runs V1->V2 migration.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
CLI_ROOT="/usr/local/lib/github-runner-tools/cli"
WEB_ROOT="/usr/local/lib/github-runner-tools/web"
UNIT="/etc/systemd/system/github-runner-tools-executor.service"
LOG_ROOT="/var/log/github-runner-tools"
LOCK="/run/lock/github-runner-tools/mutation.lock"
MODE="${1:---dry-run}"
EXPECTED="${3:-}"
[[ "$MODE" == "--dry-run" || ( "$MODE" == "--apply" && "${2:-}" == "--expected-sha" && "$EXPECTED" =~ ^[0-9a-f]{40}$ && $# -eq 3 ) ]] || {
  echo "Usage: $0 [--dry-run|--apply --expected-sha FULL_SHA]" >&2; exit 2;
}
[[ "$(id -un)" == "actions" && "$EUID" != 0 ]] || { echo "Run as actions" >&2; exit 1; }
[[ "$MODE" == "--dry-run" ]] && {
  echo "V2 CLI logger upgrade dry-run (no writes)."; exit 0;
}
[[ "$(git -C "$ROOT" rev-parse HEAD)" == "$EXPECTED" &&
   -z "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]] || {
  echo "Source revision not approved or checkout dirty" >&2; exit 1;
}
sudo -v
# Fail before mutation if this is not exactly the expected V2 deployment.
sudo grep -Fqx '# managed-by=github-runner-tools-web-v2' "$UNIT" || {
  echo "Existing Executor is not recognized V2" >&2; exit 1;
}
[[ "$(sudo stat -c '%U:%G:%a:%F' "$UNIT")" == "root:root:644:regular file" ]] ||
  { echo "Unexpected Executor Unit owner/mode" >&2; exit 1; }
sudo test ! -L "$UNIT" || exit 1
sudo grep -Fqx 'ProtectSystem=strict' "$UNIT" || exit 1
BEFORE='ReadWritePaths=/home/actions /etc/systemd/system /run/github-runner-tools /run/lock/github-runner-tools /var/lib/github-runner-tools'
AFTER="$BEFORE $LOG_ROOT"
UNIT_HAS_LOG=0
if sudo grep -Fqx "$AFTER" "$UNIT"; then
  UNIT_HAS_LOG=1
  UNIT_EXPECTED_RW="$AFTER"
elif sudo grep -Fqx "$BEFORE" "$UNIT"; then
  UNIT_EXPECTED_RW="$BEFORE"
else
  echo "Unknown Executor mount whitelist; not overwriting" >&2; exit 1
fi
# Compare complete exact installed V2 unit against the frozen generated baseline.
BASELINE="$(mktemp --suffix=.service)"
cat > "$BASELINE" <<'UNIT_EOF'
# managed-by=github-runner-tools-web-v2
[Unit]
Description=github-runner-tools bounded Web V2 Executor
After=network-online.target systemd-tmpfiles-setup.service
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
RuntimeDirectory=github-runner-tools
RuntimeDirectoryMode=0755
ExecStart=/usr/bin/python3 /usr/local/lib/github-runner-tools/web/v2_executor.py
Restart=on-failure
RestartSec=2
AmbientCapabilities=CAP_SETUID CAP_SETGID
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=/home/actions /etc/systemd/system /run/github-runner-tools /run/lock/github-runner-tools /var/lib/github-runner-tools

[Install]
WantedBy=multi-user.target
UNIT_EOF
if [[ "$UNIT_HAS_LOG" == 1 ]]; then
  /usr/bin/python3 - "$BASELINE" "$BEFORE" "$AFTER" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1])
lines=p.read_text().splitlines()
assert lines.count(sys.argv[2]) == 1
p.write_text("\n".join(sys.argv[3] if x == sys.argv[2] else x for x in lines) + "\n")
PY
fi
sudo cmp -s "$BASELINE" "$UNIT" || { echo "UNKNOWN_EXECUTOR_UNIT_BASELINE" >&2; rm -f "$BASELINE"; exit 1; }
BASELINE_SHA="$(sha256sum "$BASELINE" | cut -d' ' -f1)"
rm -f "$BASELINE"

[[ "$(sudo stat -c '%U:%G:%a:%F' "$CLI_ROOT")" == "root:root:755:directory" ]] || exit 1
[[ -f "$LOCK" && ! -L "$LOCK" ]] || exit 1
[[ "$(stat -c '%U:%G:%a' "$LOCK")" == "root:actions:660" ]] || exit 1
exec {MUTATION_LOCK}<>"$LOCK"
flock -n "$MUTATION_LOCK" || { echo "Lifecycle operation active" >&2; exit 1; }

FILES=(register-runner.sh remove-runner.sh status-runners.sh cli-operational-logger.py)
WEB_FILES=(cli_create_authority.py cli_remove_authority.py runner_lifecycle_authority.py v2_unit_installer.py)
for name in "${FILES[@]}"; do
  [[ -f "$ROOT/scripts/$name" && ! -L "$ROOT/scripts/$name" ]] || exit 1
  /usr/bin/python3 -m py_compile "$ROOT/scripts/cli-operational-logger.py"
  [[ "$name" == *.sh ]] && bash -n "$ROOT/scripts/$name"
done
[[ "$(sudo stat -c '%U:%G:%a:%F' "$WEB_ROOT")" == "root:root:755:directory" ]] || exit 1
for name in "${WEB_FILES[@]}"; do
  [[ -f "$ROOT/web/$name" && ! -L "$ROOT/web/$name" ]] || exit 1
  /usr/bin/python3 -m py_compile "$ROOT/web/$name"
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$WEB_ROOT/$name")" == "root:root:755:regular file" ]] || exit 1
done
sudo systemctl is-active --quiet github-runner-tools-executor.service || exit 1
sudo systemctl is-active --quiet github-runner-tools-web.service || exit 1
RUNNER_UNITS_BEFORE="$(systemctl list-unit-files 'actions.runner.*.service' --no-legend --no-pager | sort | sha256sum)"

# Root-only durable snapshot. It is deliberately OUTSIDE the log directory.
BACKUP="$(sudo mktemp -d /var/lib/github-runner-tools/cli-logging-upgrade.XXXXXXXX)"
sudo chmod 0700 "$BACKUP"
sudo cp -a "$UNIT" "$BACKUP/executor.service"
sudo mkdir -m 0700 "$BACKUP/cli"
for name in "${FILES[@]}"; do
  if sudo test -e "$CLI_ROOT/$name"; then
    [[ "$(sudo stat -c '%U:%G:%a:%F' "$CLI_ROOT/$name")" == "root:root:755:regular file" ]] || exit 1
    sudo cp -a "$CLI_ROOT/$name" "$BACKUP/cli/$name"
  else
    [[ "$name" == "cli-operational-logger.py" ]] || exit 1
  fi
done
sudo mkdir -m 0700 "$BACKUP/web"
for name in "${WEB_FILES[@]}"; do
  sudo cp -a "$WEB_ROOT/$name" "$BACKUP/web/$name"
done
# Stage and validate exact single-line Unit amendment before replacing.
STAGE="$(mktemp --suffix=.service)"
sudo cat "$UNIT" > "$STAGE"
/usr/bin/python3 - "$STAGE" "$UNIT_EXPECTED_RW" "$AFTER" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1]); lines=p.read_text().splitlines()
assert lines.count(sys.argv[2])==1 and not any(
    x.startswith("ReadWritePaths=") and x!=sys.argv[2] for x in lines)
p.write_text("\n".join(sys.argv[3] if x==sys.argv[2] else x for x in lines)+"\n")
PY
systemd-analyze verify "$STAGE" >/dev/null 2>&1 || { rm -f "$STAGE"; exit 1; }

COMMITTED=0
rollback() {
  local original_rc=$? failed=0 name
  if [[ "$COMMITTED" == 1 ]]; then return 0; fi
  trap - EXIT
  sudo cp -a "$BACKUP/executor.service" "$UNIT" || failed=1
  for name in "${FILES[@]}"; do
    if sudo test -f "$BACKUP/cli/$name"; then
      sudo cp -a "$BACKUP/cli/$name" "$CLI_ROOT/$name" || failed=1
    else
      sudo rm -f "$CLI_ROOT/$name" || failed=1
    fi
  done
  for name in "${WEB_FILES[@]}"; do
    sudo cp -a "$BACKUP/web/$name" "$WEB_ROOT/$name" || failed=1
  done
  sudo systemctl daemon-reload || failed=1
  sudo systemctl restart github-runner-tools-executor.service || failed=1
  sudo cmp -s "$BACKUP/executor.service" "$UNIT" || failed=1
  [[ "$(sudo sha256sum "$UNIT" | cut -d' ' -f1)" == "$BASELINE_SHA" ]] || failed=1
  for name in "${FILES[@]}"; do
    if sudo test -f "$BACKUP/cli/$name"; then
      sudo cmp -s "$BACKUP/cli/$name" "$CLI_ROOT/$name" || failed=1
    else
      sudo test ! -e "$CLI_ROOT/$name" || failed=1
    fi
  done
  for name in "${WEB_FILES[@]}"; do
    sudo cmp -s "$BACKUP/web/$name" "$WEB_ROOT/$name" || failed=1
  done
  sudo systemctl is-active --quiet github-runner-tools-executor.service || failed=1
  sudo systemctl is-active --quiet github-runner-tools-web.service || failed=1
  sudo test -S /run/github-runner-tools/executor.sock || failed=1
  sudo systemctl show github-runner-tools-executor.service -p ProtectSystem --value | grep -Fx strict >/dev/null || failed=1
  if [[ "$UNIT_HAS_LOG" == 1 ]]; then
    sudo systemctl show github-runner-tools-executor.service -p ReadWritePaths --value | grep -Fq "$LOG_ROOT" || failed=1
  else
    if sudo systemctl show github-runner-tools-executor.service -p ReadWritePaths --value | grep -Fq "$LOG_ROOT"; then
      failed=1
    fi
  fi
  # Verify the restored socket speaks the existing read-only Status protocol.
  sudo -u grt-web /usr/bin/python3 - <<'ROLLBACK_STATUS' || failed=1
import sys
sys.path.insert(0, "/usr/local/lib/github-runner-tools/web")
from v2_executor_client import fetch_status
response = fetch_status()
if response.get("ok") is not True:
    raise SystemExit(1)
ROLLBACK_STATUS
  [[ "$(systemctl list-unit-files 'actions.runner.*.service' --no-legend --no-pager | sort | sha256sum)" == "$RUNNER_UNITS_BEFORE" ]] || failed=1
  # Do not discard new operational evidence from a failed smoke.
  if [[ "${LOG_LOCK_CREATED:-0}" == 1 ]] && [[ "$(sudo find "$LOG_ROOT" -mindepth 1 -maxdepth 1 | wc -l)" == 1 ]]; then
    sudo rm -f "$LOG_ROOT/operational.lock" || failed=1
  fi
  if [[ "${LOG_DIR_CREATED:-0}" == 1 ]]; then
    sudo rmdir "$LOG_ROOT" || failed=1
  fi
  if [[ "$failed" == 1 ]]; then
    echo "ROLLBACK_INCOMPLETE: preserve backup $BACKUP and reconcile" >&2
    return 1
  fi
  echo "ROLLBACK_VERIFIED" >&2
  return "${original_rc:-1}"
}
trap rollback EXIT
LOG_DIR_CREATED=0
LOG_LOCK_CREATED=0
# Pre-existing unexpected log roots require administrator reconciliation.
if sudo test -e "$LOG_ROOT" || sudo test -L "$LOG_ROOT"; then
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$LOG_ROOT")" == "root:actions:2770:directory" ]] || exit 1
else
  sudo install -d -o root -g actions -m 2770 "$LOG_ROOT"
  LOG_DIR_CREATED=1
fi
if sudo test -e "$LOG_ROOT/operational.lock" || sudo test -L "$LOG_ROOT/operational.lock"; then
  [[ "$(sudo stat -c '%U:%G:%a:%h:%F' "$LOG_ROOT/operational.lock")" == "actions:actions:600:1:regular file" ]] || exit 1
else
  sudo install -o actions -g actions -m 0600 /dev/null "$LOG_ROOT/operational.lock"
  LOG_LOCK_CREATED=1
fi

# Install completed root-owned temp files, then rename on the same
# filesystem. Never truncate a live executable while Status can be spawned.
for name in "${FILES[@]}"; do
  TEMP_TARGET="$(sudo mktemp "$CLI_ROOT/.grt-cli-update.XXXXXXXX")"
  sudo install -o root -g root -m 0755 "$ROOT/scripts/$name" "$TEMP_TARGET"
  sudo mv -f -- "$TEMP_TARGET" "$CLI_ROOT/$name"
done
for name in "${WEB_FILES[@]}"; do
  TEMP_TARGET="$(sudo mktemp "$WEB_ROOT/.grt-web-update.XXXXXXXX")"
  sudo install -o root -g root -m 0755 "$ROOT/web/$name" "$TEMP_TARGET"
  sudo mv -f -- "$TEMP_TARGET" "$WEB_ROOT/$name"
done
TEMP_UNIT="$(sudo mktemp /etc/systemd/system/.grt-executor-update.XXXXXXXX)"
sudo install -o root -g root -m 0644 "$STAGE" "$TEMP_UNIT"
sudo mv -f -- "$TEMP_UNIT" "$UNIT"
rm -f "$STAGE"
sudo systemctl daemon-reload
sudo systemctl restart github-runner-tools-executor.service
sudo systemctl is-active --quiet github-runner-tools-executor.service
sudo systemctl is-active --quiet github-runner-tools-web.service
sudo grep -Fqx "$AFTER" "$UNIT"
[[ "$(systemctl list-unit-files 'actions.runner.*.service' --no-legend --no-pager | sort | sha256sum)" == "$RUNNER_UNITS_BEFORE" ]] || {
  echo "Production runner unit inventory changed during upgrade" >&2; exit 1;
}
# B-01 acceptance: actual root Executor spawns installed Status CLI under
# actions, subject to its mount namespace. No remote registration is invoked.
SMOKE_STARTED="$(date +%s)"
sudo -u grt-web /usr/bin/python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/local/lib/github-runner-tools/web")
from v2_executor_client import fetch_status
reply = fetch_status()
if reply.get("ok") is not True:
    raise SystemExit("Executor CLI Status smoke failed")
PY
# Status is read-only; require evidence actually produced by its CLI logger.
sudo -u actions /usr/bin/python3 - "$SMOKE_STARTED" <<'PY'
import json,sys
from pathlib import Path
from datetime import datetime
path=Path("/var/log/github-runner-tools/operational.jsonl")
started=int(sys.argv[1])
if not path.is_file():
    raise SystemExit("CLI logger produced no active file")
records=[json.loads(line) for line in path.read_text().splitlines()[-100:]]
assert any(r.get("op")=="status" and r.get("event")=="operation_exit"
           and datetime.fromisoformat(r["ts"]).timestamp()>=started-2
           for r in records), "No Executor-launched CLI status event found"
PY
for name in "${FILES[@]}"; do
  sudo cmp -s "$ROOT/scripts/$name" "$CLI_ROOT/$name" || { echo "Installed CLI dependency closure mismatch: $name" >&2; exit 1; }
done
for name in "${WEB_FILES[@]}"; do
  sudo cmp -s "$ROOT/web/$name" "$WEB_ROOT/$name" || { echo "Installed trusted dependency closure mismatch: $name" >&2; exit 1; }
done
COMMITTED=1
trap - EXIT
echo "CLI logging/remediation update installed; real Executor CLI Status log write verified."
echo "Root-only backup available at: $BACKUP"
