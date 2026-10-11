#!/usr/bin/env bash
# Controlled update of an ALREADY-INSTALLED Web V2 CLI logger and one
# Executor ReadWritePaths exception. NEVER runs V1->V2 migration.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
CLI_ROOT="/usr/local/lib/github-runner-tools/cli"
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
if sudo grep -Fqx "$AFTER" "$UNIT"; then
  echo "Logger upgrade already present: stop and reconcile installed code" >&2; exit 1
fi
sudo grep -Fqx "$BEFORE" "$UNIT" || {
  echo "Unknown Executor mount whitelist; not overwriting" >&2; exit 1;
}
[[ "$(sudo stat -c '%U:%G:%a:%F' "$CLI_ROOT")" == "root:root:755:directory" ]] || exit 1
[[ -f "$LOCK" && ! -L "$LOCK" ]] || exit 1
[[ "$(stat -c '%U:%G:%a' "$LOCK")" == "root:actions:660" ]] || exit 1
exec {MUTATION_LOCK}<>"$LOCK"
flock -n "$MUTATION_LOCK" || { echo "Lifecycle operation active" >&2; exit 1; }

FILES=(register-runner.sh remove-runner.sh status-runners.sh cli-operational-logger.py)
for name in "${FILES[@]}"; do
  [[ -f "$ROOT/scripts/$name" && ! -L "$ROOT/scripts/$name" ]] || exit 1
  /usr/bin/python3 -m py_compile "$ROOT/scripts/cli-operational-logger.py"
  [[ "$name" == *.sh ]] && bash -n "$ROOT/scripts/$name"
done
sudo systemctl is-active --quiet github-runner-tools-executor.service || exit 1
sudo systemctl is-active --quiet github-runner-tools-web.service || exit 1
RUNNER_UNITS_BEFORE="$(systemctl list-unit-files 'actions.runner.*.service' --no-legend --no-pager | sort | sha256sum)"

# Pre-existing unexpected log roots require administrator reconciliation.
if sudo test -e "$LOG_ROOT" || sudo test -L "$LOG_ROOT"; then
  [[ "$(sudo stat -c '%U:%G:%a:%F' "$LOG_ROOT")" == "root:actions:2770:directory" ]] || exit 1
else
  sudo install -d -o root -g actions -m 2770 "$LOG_ROOT"
fi
if sudo test -e "$LOG_ROOT/operational.lock" || sudo test -L "$LOG_ROOT/operational.lock"; then
  [[ "$(sudo stat -c '%U:%G:%a:%h:%F' "$LOG_ROOT/operational.lock")" == "actions:actions:600:1:regular file" ]] || exit 1
else
  sudo install -o actions -g actions -m 0600 /dev/null "$LOG_ROOT/operational.lock"
fi

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
# Stage and validate exact single-line Unit amendment before replacing.
STAGE="$(mktemp --suffix=.service)"
sudo cat "$UNIT" > "$STAGE"
/usr/bin/python3 - "$STAGE" "$BEFORE" "$AFTER" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1]); lines=p.read_text().splitlines()
assert lines.count(sys.argv[2])==1 and not any(x.startswith("ReadWritePaths=") and x!=sys.argv[2] for x in lines)
p.write_text("\n".join(sys.argv[3] if x==sys.argv[2] else x for x in lines)+"\n")
PY
systemd-analyze verify "$STAGE" >/dev/null 2>&1 || { rm -f "$STAGE"; exit 1; }

COMMITTED=0
rollback() {
  rc=$?
  if [[ "$COMMITTED" == 1 ]]; then return; fi
  echo "CLI logger upgrade incomplete; attempting code + Unit restoration" >&2
  sudo cp -a "$BACKUP/executor.service" "$UNIT" || :
  for name in "${FILES[@]}"; do
    if sudo test -f "$BACKUP/cli/$name"; then
      sudo cp -a "$BACKUP/cli/$name" "$CLI_ROOT/$name" || :
    else
      sudo rm -f "$CLI_ROOT/$name" || :
    fi
  done
  sudo systemctl daemon-reload || :
  sudo systemctl restart github-runner-tools-executor.service || :
  echo "Confirm rollback and six runner identities manually; backup at $BACKUP" >&2
  return "$rc"
}
trap rollback EXIT
for name in "${FILES[@]}"; do
  sudo install -o root -g root -m 0755 "$ROOT/scripts/$name" "$CLI_ROOT/$name"
done
sudo install -o root -g root -m 0644 "$STAGE" "$UNIT"
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
COMMITTED=1
trap - EXIT
echo "CLI logging upgrade installed; verify real Executor CLI log write before Create/Remove."
echo "Root-only backup available at: $BACKUP"
