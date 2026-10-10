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
        raw = "secret-token-FORBIDDEN\\nGRT_CREATE_DIAG_V1 stage=registration exit=1\\n"
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            lifecycle_worker.forward_create_diagnostic(raw)
        self.assertEqual(out.getvalue(), "worker_diag stage=create_registration exit=1\\n")
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
        raw = ("token-NEVER-LOG\\nworker_diag stage=create_preflight exit=1\\n"
               "worker_diag stage=create_registration exit=7 extra=secret\\n")
        with contextlib.redirect_stderr(out):
            dispatcher.forward_worker_diagnostics(raw)
        self.assertEqual(out.getvalue(), "worker_diag stage=create_preflight exit=1\\n")

    def test_shell_emits_preflight_marker_without_sensitive_text(self):
        cmd = ["bash", str(ROOT / "scripts/register-runner.sh"),
               "--web-worker", "ernestyu/github-runner-tools"]
        p = subprocess.run(cmd, text=True, capture_output=True, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("GRT_CREATE_DIAG_V1 stage=preflight exit=", p.stderr)
        self.assertNotIn("GRT_CREATE_DIAG_V1", p.stdout)


if __name__ == "__main__":
    unittest.main()
