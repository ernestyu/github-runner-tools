#!/usr/bin/env python3
"""B-01 fault injection: run actual V2 rollback helper in isolated file tree.

No sudo, real systemd, GitHub, Debian or production Runner operation.
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/web-v2-cutover-rollback.sh"

BASH = r'''
set -Eeuo pipefail
source "$HELPER"
sudo() { "$@"; }
systemctl() {
  local action="$1" unit="${2:-}"
  case "$action" in
    stop)
      shift
      for unit in "$@"; do echo stopped > "$BASE/services/$unit"; done ;;
    disable)
      echo disabled > "$BASE/services/enabled.$unit" ;;
    enable)
      echo enabled > "$BASE/services/enabled.$unit" ;;
    start)
      if [[ "$unit" == github-runner-tools-web.service &&
            -f "$BASE/fail-v1-restart" ]]; then return 1; fi
      echo running > "$BASE/services/$unit" ;;
    daemon-reload) : ;;
    is-active)
      [[ "${2:-}" == --quiet ]] && unit="$3"
      [[ "$(cat "$BASE/services/$unit" 2>/dev/null)" == running ]] ;;
    *) echo "Unexpected systemctl command: $*" >&2; return 1 ;;
  esac
}
V1_WEB_ACTIVE=1; V1_WEB_ENABLED=1
V1_DISPATCH_ACTIVE=1; V1_DISPATCH_ENABLED=1
CUTOVER_STARTED=1; CUTOVER_COMMITTED=0
trap grt_v2_rollback_on_exit EXIT
trap 'exit 130' INT
# Failure before cutover must never stop or rewrite a healthy V1.\nif [[ "$CASE" == preflight_failure ]]; then CUTOVER_STARTED=0; false; fi\n# Pretend the installer has interrupted V1 and partially installed V2:
systemctl stop github-runner-tools-web.service github-runner-tools-dispatch.service
echo v2-code > "$INSTALL_ROOT/app.py"
echo v2-unit > "$WEB_UNIT"
echo v2-cli > "$CLI_ROOT/register-runner.sh"
echo v2-config > "$CONFIG"
echo v2-unit > "$EXEC_UNIT"
if [[ "$CASE" == journal_changed ]]; then
  printf '\nchanged' >> "$STATE_DIR/active-instances/fixture.json"
fi
if [[ "$CASE" == fail_after_service_start ]]; then
  systemctl start github-runner-tools-executor.service
fi
# Intentionally fail at different points in the service cutover.
false
'''

class CutoverFaults(unittest.TestCase):
    def check_case(self, scenario):
        with tempfile.TemporaryDirectory(prefix="grt-b01-") as temp:
            base = pathlib.Path(temp)
            state = base / "state"
            backup = base / "backup"
            installed = base / "web"
            cli = base / "cli"
            etc = base / "etc"
            services = base / "services"
            for d in (state, backup, installed, cli, etc, services,
                      state/"active-instances", backup/"lifecycle-snapshot"/"active-instances",
                      backup/"installed-web", backup/"installed-cli"):
                d.mkdir(parents=True, exist_ok=True)
            record = b'{"schema":2,"stage":"PRE_REGISTRATION"}'
            (state/"active-instances"/"fixture.json").write_bytes(record)
            (backup/"lifecycle-snapshot"/"active-instances"/"fixture.json").write_bytes(record)
            (backup/"installed-web"/"app.py").write_text("v1-code")
            (backup/"installed-cli"/"register-runner.sh").write_text("v1-cli")
            (backup/"web.conf").write_text("v1-config")
            (backup/"web-auth.conf").write_text("v1-auth")
            (backup/"github-runner-tools-web.service").write_text("v1-unit")
            (installed/"app.py").write_text("v1-code")
            (cli/"register-runner.sh").write_text("v1-cli")
            (etc/"web.conf").write_text("v1-config")
            (etc/"web-auth.conf").write_text("v1-auth")
            (etc/"github-runner-tools-web.service").write_text("v1-unit")
            for name in ("github-runner-tools-web.service", "github-runner-tools-dispatch.service"):
                (services/name).write_text("running")
                (services/("enabled."+name)).write_text("enabled")
            env = {**os.environ, "BASE":str(base), "HELPER":str(HELPER),
                   "CASE":scenario, "STATE_DIR":str(state), "BACKUP":str(backup),
                   "INSTALL_ROOT":str(installed), "CLI_ROOT":str(cli),
                   "CONFIG":str(etc/"web.conf"), "AUTH":str(etc/"web-auth.conf"),
                   "WEB_UNIT":str(etc/"github-runner-tools-web.service"),
                   "EXEC_UNIT":str(etc/"github-runner-tools-executor.service")}
            outcome = subprocess.run(["bash","-c",BASH],env=env,text=True,
                                     capture_output=True,timeout=15)
            self.assertNotEqual(outcome.returncode,0)
            self.assertEqual((state/"active-instances"/"fixture.json").read_bytes(),
                             record if scenario != "journal_changed" else record+b"\nchanged")
            if scenario == "preflight_failure":
                self.assertEqual((services/"github-runner-tools-web.service").read_text().strip(),"running")
                self.assertEqual((services/"github-runner-tools-dispatch.service").read_text().strip(),"running")
                self.assertEqual((etc/"web.conf").read_text(),"v1-config")
                self.assertNotIn("V2 cutover failed",outcome.stderr)
            elif scenario == "journal_changed":
                self.assertIn("manual reconciliation",outcome.stderr)
                self.assertNotEqual((services/"github-runner-tools-web.service").read_text(),"running")
                self.assertEqual((etc/"web.conf").read_text().strip(),"v2-config")
            else:
                self.assertIn("V1 Web restored",outcome.stderr)
                self.assertEqual((services/"github-runner-tools-web.service").read_text().strip(),"running")
                self.assertEqual((services/"github-runner-tools-dispatch.service").read_text().strip(),"running")
                self.assertEqual((etc/"web.conf").read_text(),"v1-config")
                self.assertEqual((etc/"github-runner-tools-web.service").read_text(),"v1-unit")
                self.assertEqual((installed/"app.py").read_text(),"v1-code")
                self.assertEqual((cli/"register-runner.sh").read_text(),"v1-cli")
                self.assertFalse((etc/"github-runner-tools-executor.service").exists())

    def test_preflight_failure_leaves_v1_running(self):
        self.check_case("preflight_failure")

    def test_failure_immediately_after_stopping_v1(self):
        self.check_case("fail_after_stop")

    def test_failure_after_v2_service_start(self):
        self.check_case("fail_after_service_start")

    def test_changed_journal_disables_automatic_rollback(self):
        self.check_case("journal_changed")

if __name__ == "__main__":
    unittest.main()
