#!/usr/bin/env python3
"""Black-box fault injection for the complete CLI logger updater transaction.

Every test runs a copy of the production entrypoint, not a sourced rollback().
Absolute paths are rewritten ONLY in the disposable test copy; the installed
updater has no environment-driven path or privilege overrides. The mocked
external commands never reach the host systemd or production runner units.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/update-cli-operational-logging-v2.sh"
SHA = "a" * 40
FILES = ("register-runner.sh", "remove-runner.sh", "status-runners.sh",
         "cli-operational-logger.py")
WEB_FILES = ("cli_create_authority.py", "cli_remove_authority.py",
             "runner_lifecycle_authority.py", "v2_unit_installer.py")
UNIT_BASE = re.search(r"cat > \"\$BASELINE\" <<'UNIT_EOF'\n(.*?)\nUNIT_EOF",
                      SOURCE.read_text(), re.S).group(1) + "\n"

# All subprocesses are intercepted through PATH. The sudo mock dispatches
# ordinary file operations to unprivileged programs under a temporary root.
MOCK = r'''#!/usr/bin/env bash
set -euo pipefail
cmd="${0##*/}"
if [[ "$cmd" == id ]]; then echo actions; exit 0; fi
if [[ "$cmd" == git ]]; then
  if [[ " $* " == *" rev-parse HEAD "* ]]; then echo "$APPROVED_SHA"; fi
  exit 0
fi
if [[ "$cmd" == stat && "${1:-}" == -c ]]; then
  case "${@: -1}" in
    "$TEST_UNIT") echo "root:root:644:regular file";;
    "$TEST_CLI") echo "root:root:755:directory";;
    "$TEST_LOCK") echo "root:actions:660";;
    "$TEST_LOG") echo "root:actions:2770:directory";;
    "$TEST_LOG/operational.lock") echo "actions:actions:600:1:regular file";;
    *) echo "root:root:755:regular file";;
  esac
  exit 0
fi
if [[ "$cmd" == systemd-analyze ]]; then exit 0; fi
if [[ "$cmd" == systemctl ]]; then
  case "${1:-}" in
    list-unit-files) cat "$RUNNER_UNITS"; exit 0;;
    is-active) [[ "$(cat "$SERVICE_STATE")" == active ]]; exit $?;;
    show)
      if [[ "$*" == *ProtectSystem* ]]; then echo strict
      elif grep -Fq "$TEST_LOG" "$TEST_UNIT"; then
        echo "/home/actions $TEST_LOG"
      else echo "/home/actions"; fi
      exit 0;;
    daemon-reload)
      if [[ "$FAULT" == daemon && ! -e "$FAULT_FIRED" ]]; then
        touch "$FAULT_FIRED"; exit 1
      fi
      exit 0;;
    restart)
      if [[ "$FORCE_RESTART_FAILURE" == 1 && ! -e "$FAULT_FIRED" ]]; then
        touch "$FAULT_FIRED"; echo inactive > "$SERVICE_STATE"; exit 1
      fi
      if [[ "$FAULT" == restart && ! -e "$FAULT_FIRED" ]]; then
        touch "$FAULT_FIRED"; echo inactive > "$SERVICE_STATE"; exit 1
      fi
      if [[ "$FAULT" == rollback_restart ]]; then
        if [[ ! -e "$FAULT_FIRED" ]]; then touch "$FAULT_FIRED"; exit 1; fi
        echo inactive > "$SERVICE_STATE"; exit 1
      fi
      echo active > "$SERVICE_STATE"; exit 0;;
  esac
  exit 2
fi
if [[ "$cmd" != sudo ]]; then
  echo "Unknown mocked command $cmd" >&2; exit 2
fi
[[ "${1:-}" == -v ]] && exit 0
if [[ "${1:-}" == -u ]]; then
  user="$2"; shift 2
  # The real Executor client is never contacted by a test.
  if [[ "$user" == grt-web ]]; then
    cat >/dev/null
    if [[ "$FAULT" == smoke && ! -e "$FAULT_FIRED" ]]; then
      touch "$FAULT_FIRED"; exit 1
    fi
    if [[ "$FAULT" == rollback_status && -e "$FAULT_FIRED" ]]; then exit 1; fi
    if grep -Fq "$TEST_LOG" "$TEST_UNIT"; then
      /usr/bin/python3 - "$TEST_LOG" <<'LOGPY'
import datetime, json, pathlib, sys
p=pathlib.Path(sys.argv[1])/"operational.jsonl"
p.parent.mkdir(parents=True,exist_ok=True)
with p.open("a") as f:
 f.write(json.dumps({"op":"status","event":"operation_exit",
                     "ts":datetime.datetime.now(datetime.timezone.utc).isoformat()})+"\n")
LOGPY
    fi
    exit 0
  fi
  if [[ "$user" == actions ]]; then
    # Execute the actual smoke checker against relocated paths.
    "$@"; exit $?
  fi
  exit 2
fi
operation="$1"; shift
case "$operation" in
  stat)
    exec "$MOCK_DIR/stat" "$@";;
  systemctl)
    exec "$MOCK_DIR/systemctl" "$@";;
  mktemp|mkdir|chmod|cat|grep|cmp|test|find|rmdir|rm|sha256sum)
    exec "$operation" "$@";;
  install)
    if [[ "$FAULT" == log_init && "$*" == *operational.lock* &&
          ! -e "$FAULT_FIRED" ]]; then
      touch "$FAULT_FIRED"; exit 1
    fi
    args=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -o|-g) shift 2;;
        *) args+=("$1"); shift;;
      esac
    done
    exec install "${args[@]}";;
  cp)
    if [[ "$FAULT" == backup && "$*" == *"$TEST_BACKUPS"* &&
          ! -e "$FAULT_FIRED" ]]; then touch "$FAULT_FIRED"; exit 1; fi
    if [[ "$FAULT" == rollback_cp && -e "$FAULT_FIRED" &&
          "$*" == *executor.service* ]]; then exit 1; fi
    exec cp "$@";;
  mv)
    if [[ "$FAULT" == cli_install && "$*" == *register-runner.sh* &&
          ! -e "$FAULT_FIRED" ]]; then touch "$FAULT_FIRED"; exit 1; fi
    if [[ "$FAULT" == unit_replace && "$*" == *executor.service* &&
          ! -e "$FAULT_FIRED" ]]; then touch "$FAULT_FIRED"; exit 1; fi
    exec mv "$@";;
  *)
    echo "Unexpected sudo operation: $operation" >&2; exit 2;;
esac
'''
class TransactionFaultTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(prefix="grt-updater-")
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name)
        self.repo = self.root / "repo"
        self.scripts = self.repo / "scripts"
        self.scripts.mkdir(parents=True)
        self.cli = self.root / "installed-cli"
        self.cli.mkdir()
        self.unit = self.root / "executor.service"
        self.baseline_unit = UNIT_BASE.replace("/usr/local/lib/github-runner-tools/web", str(self.root / "web"))
        self.unit.write_text(self.baseline_unit)
        self.log = self.root / "log"
        self.lock = self.root / "mutation.lock"
        self.lock.touch()
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.web = self.root / "web"
        self.web.mkdir()
        self.source_web = self.repo / "web"
        self.source_web.mkdir()
        self.sock_path = self.root / "executor.sock"
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.sock_path))
        self.addCleanup(self.sock.close)
        self.state = self.root / "state"
        self.state.write_text("active")
        self.runner_units = self.root / "runner-units"
        self.runner_units.write_text("".join(
            f"actions.runner.prod-{i}.service enabled\n" for i in range(6)))
        self.original_units = self.runner_units.read_bytes()
        for name in FILES:
            if name.endswith(".py"):
                new = "#!/usr/bin/env python3\nprint('new')\n"
                old = None
            else:
                new = "#!/usr/bin/env bash\necho new\n"
                old = "#!/usr/bin/env bash\necho old\n"
            (self.scripts / name).write_text(new)
            if old:
                (self.cli / name).write_text(old)
        self.original = {p.name: p.read_bytes() for p in self.cli.iterdir()}
        for name in WEB_FILES:
            (self.source_web / name).write_text("#!/usr/bin/env python3\nprint('new-web')\n")
            (self.web / name).write_text("#!/usr/bin/env python3\nprint('old-web')\n")
        self.original_web = {p.name: p.read_bytes() for p in self.web.iterdir()}
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "mock").write_text(MOCK)
        (self.bin / "mock").chmod(0o755)
        for command in ("sudo", "systemctl", "git", "id", "stat", "systemd-analyze"):
            (self.bin / command).symlink_to("mock")
        source = SOURCE.read_text()
        substitutions = (
            ("/usr/local/lib/github-runner-tools/cli", str(self.cli)),
            ("/etc/systemd/system/github-runner-tools-executor.service", str(self.unit)),
            ("/etc/systemd/system/.grt-executor-update.", str(self.root / ".grt-executor-update.")),
            ("/var/log/github-runner-tools", str(self.log)),
            ("/run/lock/github-runner-tools/mutation.lock", str(self.lock)),
            ("/var/lib/github-runner-tools/cli-logging-upgrade.", str(self.backups / "cli-logging-upgrade.")),
            ("/run/github-runner-tools/executor.sock", str(self.sock_path)),
            ("/usr/local/lib/github-runner-tools/web", str(self.web)),
        )
        for before, after in substitutions:
            source = source.replace(before, after)
        (self.scripts / SOURCE.name).write_text(source)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        MOCK_DIR=str(self.bin), APPROVED_SHA=SHA,
                        TEST_UNIT=str(self.unit), TEST_CLI=str(self.cli),
                        TEST_LOCK=str(self.lock), TEST_LOG=str(self.log),
                        TEST_BACKUPS=str(self.backups),
                        SERVICE_STATE=str(self.state), RUNNER_UNITS=str(self.runner_units),
                        FAULT_FIRED=str(self.root / "fault-fired"), FAULT="none",
                        FORCE_RESTART_FAILURE="0")

    def invoke(self, fault="none", prepare=None):
        self.env["FAULT"] = fault
        if prepare:
            prepare()
        return subprocess.run(
            ["bash", str(self.scripts / SOURCE.name), "--apply", "--expected-sha", SHA],
            env=self.env, capture_output=True, text=True, timeout=20)

    def backups_found(self):
        return sorted(self.backups.glob("cli-logging-upgrade.*"))

    def assert_original(self):
        self.assertEqual(self.unit.read_text(), self.baseline_unit)
        self.assertEqual({p.name: p.read_bytes() for p in self.cli.iterdir()
                          if not p.name.startswith(".grt-cli-update")}, self.original)
        self.assertEqual(self.state.read_text().strip(), "active")
        self.assertEqual(self.runner_units.read_bytes(), self.original_units)
        self.assertEqual({p.name: p.read_bytes() for p in self.web.iterdir()
                          if not p.name.startswith(".grt-web-update")},
                         self.original_web)

    def test_success_commits_complete_transaction(self):
        p = self.invoke()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(str(self.log), self.unit.read_text())
        self.assertTrue((self.log / "operational.jsonl").is_file())
        self.assertEqual(len(self.backups_found()), 1)
        self.assertEqual(self.runner_units.read_bytes(), self.original_units)
        for name in WEB_FILES:
            self.assertEqual((self.web / name).read_bytes(),
                             (self.source_web / name).read_bytes())

    def test_preflight_cannot_mutate_installation(self):
        self.unit.write_text(self.baseline_unit.replace("ProtectSystem=strict", "ProtectSystem=full"))
        before = self.unit.read_bytes()
        p = self.invoke()
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.unit.read_bytes(), before)
        self.assertEqual(len(self.backups_found()), 0)
        self.assertEqual(self.state.read_text(), "active")
        self.assertEqual(self.runner_units.read_bytes(), self.original_units)

    def test_backup_failure_precedes_any_installation_changes(self):
        p = self.invoke("backup")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("ROLLBACK_VERIFIED", p.stderr)
        self.assert_original()
        self.assertEqual(self.state.read_text().strip(), "active")

    def test_mutation_faults_trigger_exit_trap_and_restore(self):
        for fault in ("log_init", "cli_install", "unit_replace",
                      "daemon", "restart", "smoke"):
            with self.subTest(fault=fault):
                self.setUp_isolated_case(fault)

    def setUp_isolated_case(self, fault):
        # Each subcase is fully isolated; never reuse a prior transaction.
        case = self.__class__("runTest")
        case.setUp()
        try:
            p = case.invoke(fault)
            self.assertNotEqual(p.returncode, 0, (fault, p.stdout, p.stderr))
            self.assertIn("ROLLBACK_VERIFIED", p.stderr, (fault, p.stderr))
            case.assert_original()
            self.assertEqual(len(case.backups_found()), 1)
            self.assertEqual(case.runner_units.read_bytes(), case.original_units)
        finally:
            case.doCleanups()

    def test_failed_rollback_copy_is_incomplete(self):
        # Restart failure creates the first fault, then restoration copy fails.
        p = self.invoke("rollback_cp", prepare=lambda: self.env.update(
            FAULT="rollback_cp", FORCE_RESTART_FAILURE="1"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("ROLLBACK_INCOMPLETE", p.stderr)
        self.assertNotIn("ROLLBACK_VERIFIED", p.stderr)
        self.assertEqual(len(self.backups_found()), 1)
        self.assertEqual(self.runner_units.read_bytes(), self.original_units)

    def test_failed_rollback_restart_is_incomplete(self):
        p = self.invoke("rollback_restart")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("ROLLBACK_INCOMPLETE", p.stderr)
        self.assertNotIn("ROLLBACK_VERIFIED", p.stderr)
        self.assertEqual(self.state.read_text().strip(), "inactive")
        self.assertEqual(len(self.backups_found()), 1)

    def test_preexisting_log_evidence_is_not_removed(self):
        self.log.mkdir()
        (self.log / "operational.lock").touch()
        evidence = self.log / "operational.jsonl"
        evidence.write_bytes(b'{"evidence":"before"}\n')
        p = self.invoke("restart")
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(evidence.exists())
        self.assertIn(b'{"evidence":"before"}', evidence.read_bytes())
        self.assertEqual(self.runner_units.read_bytes(), self.original_units)

if __name__ == "__main__":
    unittest.main()
