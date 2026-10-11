#!/usr/bin/env python3
"""Execute the updater's REAL Bash rollback function in an isolated file tree.

No host systemd, sudo privileges, production paths or Runner services are touched.
The shell functions below replace only privileged system commands; the rollback
body is extracted byte-for-byte from the reviewed production updater.
"""
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" /
          "update-cli-operational-logging-v2.sh").read_text()
START = SCRIPT.index("rollback() {")
END = SCRIPT.index("\ntrap rollback EXIT", START)
ROLLBACK = SCRIPT[START:END]
FILES = ("register-runner.sh", "remove-runner.sh", "status-runners.sh",
         "cli-operational-logger.py")

class UpdaterFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = self.root / "installed-cli"
        self.backup = self.root / "backup"
        self.log = self.root / "log"
        self.cli.mkdir()
        (self.backup / "cli").mkdir(parents=True)
        self.log.mkdir()
        self.unit = self.root / "executor.service"
        self.original = b"# managed-by=github-runner-tools-web-v2\nProtectSystem=strict\n"
        self.unit.write_bytes(self.original)
        (self.backup / "executor.service").write_bytes(self.original)
        for n in FILES[:-1]:
            (self.cli/n).write_text("new-" + n)
            (self.backup/"cli"/n).write_text("old-" + n)
        (self.cli/FILES[-1]).write_text("new logger")
        self.sock=socket.socket(socket.AF_UNIX)
        self.socket_path = self.root/"executor.sock"
        self.sock.bind(str(self.socket_path))
        self.addCleanup(self.sock.close)

    def run_rollback(self, fail="", init=False):
        # Replace the fixed socket locator in an extracted copy ONLY for the
        # isolated harness; production rollback logic is otherwise unmodified.
        source = ROLLBACK.replace(
            "/run/github-runner-tools/executor.sock", str(self.socket_path))
        # The live check of absent ReadWritePaths must still run; unit fixture
        # contains no newly authorized log root.
        bootstrap = r'''
set +e
CLI_ROOT="$TEST_CLI"; UNIT="$TEST_UNIT"; BACKUP="$TEST_BACKUP"
LOG_ROOT="$TEST_LOG"; BASELINE_SHA="$TEST_SHA"
RUNNER_UNITS_BEFORE="$(printf 'actions.runner.test.service enabled\\n' | sha256sum)"
FILES=(register-runner.sh remove-runner.sh status-runners.sh cli-operational-logger.py)
COMMITTED=0
LOG_LOCK_CREATED="$TEST_LOCK_CREATED"
LOG_DIR_CREATED="$TEST_DIR_CREATED"
sudo() {
  if [[ "$1" == "-u" ]]; then
    cat >/dev/null
    [[ "$TEST_FAIL" == "status" ]] && return 1
    return 0
  fi
  if [[ "$1" == "cp" && "$TEST_FAIL" == "restore" ]]; then
    return 1
  fi
  "$@"
}
systemctl() {
  case "$1" in
    daemon-reload) [[ "$TEST_FAIL" == "daemon-reload" ]] && return 1; return 0 ;;
    restart) [[ "$TEST_FAIL" == "restart" ]] && return 1; return 0 ;;
    is-active) return 0 ;;
    show)
      if [[ "$*" == *"ProtectSystem"* ]]; then printf 'strict\\n'
      else printf '/home/actions /etc/systemd/system\\n'
      fi ;;
    list-unit-files) printf 'actions.runner.test.service enabled\\n' ;;
    *) return 2 ;;
  esac
}
'''
        # keep test output deterministic; emulate rollback trigger from failed update
        suffix='\nfalse\nrollback\nexit $?\n'
        env={**os.environ,
             "TEST_CLI":str(self.cli),"TEST_UNIT":str(self.unit),
             "TEST_BACKUP":str(self.backup),"TEST_LOG":str(self.log),
             "TEST_SHA":hashlib.sha256(self.original).hexdigest(),
             "TEST_FAIL":fail,
             "TEST_LOCK_CREATED":"1" if init else "0",
             "TEST_DIR_CREATED":"1" if init else "0"}
        return subprocess.run(["bash","-c",bootstrap+source+suffix],
                              env=env,capture_output=True,text=True,timeout=12)

    def test_recover_after_unit_replacement_or_restart_failure(self):
        for failure in ("unit-replace","daemon-reload","restart"):
            with self.subTest(failure=failure):
                self.setUpFilesChanged()
                p=self.run_rollback()
                self.assertIn("ROLLBACK_VERIFIED",p.stderr,p.stderr)
                self.assertEqual(self.unit.read_bytes(),self.original)
                for n in FILES[:-1]:
                    self.assertEqual((self.cli/n).read_text(),"old-"+n)
                self.assertFalse((self.cli/FILES[-1]).exists())

    def setUpFilesChanged(self):
        self.unit.write_text("new executor unit")
        for n in FILES[:-1]:
            (self.cli/n).write_text("new-"+n)
        (self.cli/FILES[-1]).write_text("new logger")

    def test_rollback_failure_is_reported_incomplete(self):
        self.setUpFilesChanged()
        p=self.run_rollback(fail="restore")
        self.assertIn("ROLLBACK_INCOMPLETE",p.stderr)
        self.assertNotIn("ROLLBACK_VERIFIED",p.stderr)
        self.assertTrue((self.backup/"executor.service").exists())

    def test_service_restart_failure_keeps_backup_and_reports_incomplete(self):
        self.setUpFilesChanged()
        p=self.run_rollback(fail="restart")
        self.assertIn("ROLLBACK_INCOMPLETE",p.stderr)
        self.assertTrue((self.backup/"executor.service").exists())

    def test_socket_or_status_failure_cannot_claim_verified(self):
        self.setUpFilesChanged()
        p=self.run_rollback(fail="status")
        self.assertIn("ROLLBACK_INCOMPLETE",p.stderr)
        self.assertNotIn("ROLLBACK_VERIFIED",p.stderr)

    def test_partial_log_initialization_is_removed_if_empty(self):
        self.setUpFilesChanged()
        (self.log/"operational.lock").write_bytes(b"")
        p=self.run_rollback(init=True)
        self.assertIn("ROLLBACK_VERIFIED",p.stderr,p.stderr)
        self.assertFalse(self.log.exists())

    def test_log_evidence_is_retained_if_initialization_partially_succeeded(self):
        self.setUpFilesChanged()
        (self.log/"operational.lock").write_bytes(b"")
        (self.log/"operational.jsonl").write_text('{"v":1}\n')
        p=self.run_rollback(init=True)
        self.assertIn("ROLLBACK_INCOMPLETE",p.stderr)
        self.assertTrue((self.log/"operational.jsonl").exists())

if __name__ == "__main__":
    unittest.main()
