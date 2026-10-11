#!/usr/bin/env python3
"""Real subprocess proof for trusted Authority exit-code -> CLI -> JSONL transport."""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "scripts/register-runner.sh"
LOGGER = ROOT / "scripts/cli-operational-logger.py"
AUTHORITY = ROOT / "web/cli_create_authority.py"


class AuthorityTransportTests(unittest.TestCase):
    def setUp(self):
        probe = subprocess.run(["sudo", "-n", "true"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("passwordless sudo required for exact root Authority subprocess")
        self.tmp = tempfile.TemporaryDirectory(prefix="grt-authority-transport-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(self._restore_tmp_ownership)
        self.user = pwd.getpwuid(os.getuid())
        self.repo = f"fixture/transport-{os.getpid()}"
        self.local_id = f"fixture--transport-{os.getpid()}"
        self.runner_dir = Path(self.user.pw_dir) / ("actions-runner-" + self.local_id)
        if self.runner_dir.exists() or self.runner_dir.is_symlink():
            self.skipTest("canonical transport fixture path already exists")
        self.runner_dir.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.runner_dir, True)

        self.auth = self.root / "authority"
        self.auth.mkdir()
        shutil.copy2(AUTHORITY, self.auth / "cli_create_authority.py")
        (self.auth / "grt_web_common.py").write_text(textwrap.dedent("""
            def validate_repository(value):
                if "/" not in value:
                    raise ValueError("bad repo")
                return value
            def make_local_id(owner, project):
                return owner + "--" + project
            def canonical_service_name(repo, runner):
                return "actions.runner.fixture.service"
        """))
        (self.auth / "runner_lifecycle_authority.py").write_text(textwrap.dedent("""
            import os
            class AuthorityError(RuntimeError):
                pass
            CREATE_STATES="create-state"
            def _safe_directory(path):
                return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            def _current(*args): return None
            def _cycle_record(*args): return None
            def create_attestation(*args): pass
            def create_stage(*args): pass
            def create_unit_attestation(*args): pass
            def normalize_new_registration(*args): pass
            def verify_service_install_provenance(*args): pass
        """))
        (self.auth / "v2_unit_installer.py").write_text(textwrap.dedent("""
            import sys
            class UnitInstallError(RuntimeError):
                def __init__(self, reason):
                    super().__init__(reason)
                    self.reason=reason
            def install_canonical_unit(repo, runner, directory, user):
                print("TOKEN_SHOULD_NOT_CONTROL_REASON authority_install_failed", file=sys.stderr)
                if runner == "exit64":
                    raise UnitInstallError("runner_service_entrypoint_missing")
                if runner == "exit73":
                    raise UnitInstallError("authority_install_failed")
                raise UnitInstallError("unrecognized-free-text")
        """))
        exact = self.auth / "cli_create_authority.py"
        exact.chmod(0o755)
        subprocess.run(["sudo", "-n", "chown", "root:root", str(exact)], check=True)

        self.exit99 = self.root / "authority99.py"
        self.exit99.write_text("#!/usr/bin/env python3\nimport sys\nprint('runner_service_entrypoint_missing TOKEN_FAKE', file=sys.stderr)\nsys.exit(99)\n")
        self.exit99.chmod(0o755)
        subprocess.run(["sudo", "-n", "chown", "root:root", str(self.exit99)], check=True)

    def _restore_tmp_ownership(self):
        if hasattr(self, "root") and self.root.exists():
            subprocess.run(
                ["sudo", "-n", "chown", "-R",
                 f"{os.getuid()}:{os.getgid()}", str(self.root)],
                capture_output=True)

    def run_case(self, runner, authority):
        case = self.root / runner
        case.mkdir()
        script = case / "register-runner.sh"
        source = REGISTER.read_text().replace(
            "/usr/local/lib/github-runner-tools/web/cli_create_authority.py",
            str(authority),
        )
        script.write_text(source)
        shutil.copy2(LOGGER, case / "cli-operational-logger.py")
        script.chmod(0o755)
        code = textwrap.dedent(f"""
            export GITHUB_ACTIONS=true
            export RUNNER_TEMP={case!s}/runner-temp
            mkdir -p "$RUNNER_TEMP"
            export RUNNER_TOOLS_LIB_ONLY=1
            source {script!s}
            REPO={self.repo}
            RUNNER_NAME={runner}
            RUNNER_DIR={self.runner_dir!s}
            GRT_LOG_READY=1
            GRT_LOG_REPO="$REPO"
            GRT_LOG_RUNNER="$RUNNER_NAME"
            GRT_LOG_STAGE=REGISTERED_UNIT_INCOMPLETE
            GRT_LOG_EVENT=service_install
            GRT_LOG_SIDE_EFFECT=1
            trap 'rc=$?; grt_log_finish "$rc"' EXIT
            cli_create_authority install - 2.338.0
        """)
        proc = subprocess.run(
            ["bash", "-c", code], capture_output=True, text=True, timeout=20)
        log = case / "runner-temp" / f"grt-native-logging-{os.getuid()}" / "operational.jsonl"
        self.assertTrue(log.is_file(), proc.stderr)
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        return proc, rows

    def assert_reason(self, rows, reason):
        failed = [r for r in rows if r["event"] == "service_install" and r["result"] == "failed"]
        final = [r for r in rows if r["event"] == "operation_exit"]
        self.assertEqual(failed[-1]["reason"], reason)
        self.assertEqual(final[-1]["reason"], reason)
        encoded = json.dumps(rows)
        self.assertNotIn("TOKEN_SHOULD_NOT_CONTROL_REASON", encoded)
        self.assertNotIn("TOKEN_FAKE", encoded)

    def test_exact_authority_exit64_maps_through_shell_to_jsonl(self):
        proc, rows = self.run_case("exit64", self.auth / "cli_create_authority.py")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("TOKEN_SHOULD_NOT_CONTROL_REASON", proc.stderr)
        self.assert_reason(rows, "runner_service_entrypoint_missing")

    def test_exact_authority_exit73_maps_through_shell_to_jsonl(self):
        proc, rows = self.run_case("exit73", self.auth / "cli_create_authority.py")
        self.assertNotEqual(proc.returncode, 0)
        self.assert_reason(rows, "authority_install_failed")

    def test_unmapped_subprocess_exit_falls_back_without_stderr_parsing(self):
        proc, rows = self.run_case("exit99", self.exit99)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("runner_service_entrypoint_missing TOKEN_FAKE", proc.stderr)
        self.assert_reason(rows, "authority_install_failed")


if __name__ == "__main__":
    unittest.main()
