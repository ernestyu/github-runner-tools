#!/usr/bin/env python3
"""Bounded Create diagnostics: fixed metadata only, no raw stderr forwarding."""
import contextlib
import io
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
import dispatcher
import lifecycle_worker


class CreateDiagnosticTests(unittest.TestCase):
    def test_worker_accepts_only_whitelisted_marker(self):
        raw = "secret-token-FORBIDDEN\nGRT_CREATE_DIAG_V1 stage=registration exit=1\n"
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            lifecycle_worker.forward_create_diagnostic(raw)
        self.assertEqual(out.getvalue(), "worker_diag stage=create_registration exit=1\n")
        self.assertNotIn("secret-token", out.getvalue())

    def test_worker_rejects_untrusted_text(self):
        invalid = [
            "GRT_CREATE_DIAG_V1 stage=registration exit=0",
            "GRT_CREATE_DIAG_V1 stage=registration exit=256",
            "GRT_CREATE_DIAG_V1 stage=registration exit=1 token=secret",
            "GRT_CREATE_DIAG_V1 stage=other exit=1",
            "GRT_CREATE_DIAG_V1 stage=registration exit=-1",
        ]
        for line in invalid:
            out = io.StringIO()
            with contextlib.redirect_stderr(out):
                lifecycle_worker.forward_create_diagnostic(line)
            self.assertEqual(out.getvalue(), "")

    def test_dispatcher_forwards_whitelist_only(self):
        out = io.StringIO()
        raw = ("token-NEVER-LOG\nworker_diag stage=create_preflight exit=1\n"
               "worker_diag stage=create_registration exit=7 extra=secret\n")
        with contextlib.redirect_stderr(out):
            dispatcher.forward_worker_diagnostics(raw)
        self.assertEqual(out.getvalue(), "worker_diag stage=create_preflight exit=1\n")


    def test_cross_process_registration_failure_redacts_all_child_output(self):
        """Real Bash -> worker subprocess -> dispatcher, without network or runner."""
        import os
        import tempfile

        sentinel = "FAKE_REGISTRATION_TOKEN_NEVER_LOG"
        with tempfile.TemporaryDirectory() as tmp:
            fixture = pathlib.Path(tmp)
            # The shell process sources the production registration script in
            # library mode, sets the actual registration stage and exits via
            # its production cleanup trap. No GitHub API or local mutation.
            bash_script = fixture / "fail-registration.sh"
            bash_script.write_text(
                'set -Eeuo pipefail\n'
                'export RUNNER_TOOLS_LIB_ONLY=1\n'
                'source "$1"\n'
                'WEB_MODE=1\n'
                'CREATE_DIAG_STAGE=registration\n'
                'trap cleanup EXIT\n'
                'printf "%s\\n" "$2" >&2\n'
                'printf "traceback: synthetic failure\\n" >&2\n'
                'printf "GRT_CREATE_DIAG_V1 stage=service_start exit=1 token=%s\\n" "$2" >&2\n'
                'exit 23\n',
                encoding="utf-8",
            )
            worker = fixture / "worker-fixture.py"
            worker.write_text(
                'import contextlib, io, json, pathlib, subprocess, sys\n'
                'sys.path.insert(0, sys.argv[1])\n'
                'import lifecycle_worker\n'
                'p = subprocess.run(["bash", sys.argv[2], sys.argv[3], sys.argv[4]],'
                ' capture_output=True, text=True, check=False)\n'
                'assert p.returncode == 23\n'
                'lifecycle_worker.forward_create_diagnostic(p.stderr)\n'
                'print(json.dumps({"ok": False, "error": "lifecycle_failed"}))\n',
                encoding="utf-8",
            )
            # Dispatcher receives only worker stderr. Exercise the production
            # diagnostic forwarding function, not a copied parser.
            worker_proc = subprocess.run(
                [sys.executable, str(worker), str(ROOT / "web"),
                 str(bash_script), str(ROOT / "scripts/register-runner.sh"),
                 sentinel],
                capture_output=True, text=True, check=False,
                timeout=10,
            )
            self.assertEqual(worker_proc.returncode, 0, worker_proc.stderr)
            self.assertEqual(
                worker_proc.stdout.strip(),
                '{"ok": false, "error": "lifecycle_failed"}',
            )
            journal = io.StringIO()
            with contextlib.redirect_stderr(journal):
                dispatcher.forward_worker_diagnostics(worker_proc.stderr)
            self.assertEqual(
                journal.getvalue(),
                "worker_diag stage=create_registration exit=23\n",
            )
            self.assertNotIn(sentinel, journal.getvalue())
            self.assertNotIn("traceback", journal.getvalue())
            self.assertNotIn("service_start", journal.getvalue())
            self.assertNotIn("token=", journal.getvalue())

    def test_shell_emits_preflight_marker_without_sensitive_text(self):
        cmd = ["bash", str(ROOT / "scripts/register-runner.sh"),
               "--web-worker", "ernestyu/github-runner-tools"]
        p = subprocess.run(cmd, text=True, capture_output=True, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("GRT_CREATE_DIAG_V1 stage=preflight exit=", p.stderr)
        self.assertNotIn("GRT_CREATE_DIAG_V1", p.stdout)


if __name__ == "__main__":
    unittest.main()
