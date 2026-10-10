#!/usr/bin/env python3
"""B-01: normal CLI Create cannot delete or repurpose an existing Runner tree."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "scripts/register-runner.sh"


class CleanIncompleteBlockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "actions-runner-example--project"
        self.dir.mkdir()
        self.evidence = self.dir / "historical-job-artifact"
        self.evidence.write_bytes(b"KEEP HISTORICAL DATA\x00\x01")
        self.saved_dir = self.dir.stat()
        self.saved_file = self.evidence.stat()

    def assert_unchanged(self):
        self.assertTrue(self.dir.is_dir())
        self.assertEqual(self.dir.stat().st_ino, self.saved_dir.st_ino)
        self.assertEqual(self.evidence.stat().st_ino, self.saved_file.st_ino)
        self.assertEqual(self.evidence.read_bytes(), b"KEEP HISTORICAL DATA\x00\x01")

    def run_readonly_preflight(self):
        env = dict(os.environ, RUNNER_TOOLS_LIB_ONLY="1")
        return subprocess.run(
            ["bash", "-c",
             'source "$1"; create_target_preflight "$2"',
             "b01-preflight", str(REGISTER), str(self.dir)],
            capture_output=True, text=True, env=env, timeout=10)

    def test_unidentified_incomplete_directory_preserved(self):
        result = self.run_readonly_preflight()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Existing non-empty Runner directory", result.stderr)
        self.assert_unchanged()

    def test_unknown_after_partial_unregister_preserved(self):
        (self.dir / ".service").write_text("previous-systemd-identity\n")
        (self.dir / ".grt-service-reconcile-preserved").write_text("unknown-outcome\n")
        result = self.run_readonly_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.dir / ".service").exists())
        self.assertTrue((self.dir / ".grt-service-reconcile-preserved").exists())
        self.assert_unchanged()

    def test_incomplete_prior_create_preserved(self):
        (self.dir / ".credentials").write_text("SYNTHETIC_NON_SECRET")
        result = self.run_readonly_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.dir / ".credentials").read_text(), "SYNTHETIC_NON_SECRET")
        self.assert_unchanged()

    def test_symlink_target_rejected_without_modifying_referent(self):
        other = self.dir.parent / "actions-runner-link"
        other.symlink_to(self.dir, target_is_directory=True)
        env = dict(os.environ, RUNNER_TOOLS_LIB_ONLY="1")
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; create_target_preflight "$2"',
             "b01-link", str(REGISTER), str(other)],
            capture_output=True, text=True, env=env, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(other.is_symlink())
        self.assert_unchanged()

    def test_deprecated_clean_incomplete_cli_flag_cannot_enter_create(self):
        # Run the real parser under an unprivileged UID when CI is root.
        # Rejecting this flag happens before any token, download, or registration.
        env = dict(os.environ, RUNNER_BASE_DIR=str(self.dir.parent))
        cmd = ["bash", str(REGISTER), "--clean-incomplete", "example/project"]
        if os.geteuid() == 0:
            nobody = pwd.getpwnam("nobody")
            def demote():
                os.setgroups([])
                os.setgid(nobody.pw_gid)
                os.setuid(nobody.pw_uid)
            # tmp directories are 0700 by design; the parser's rejection does
            # not need access to target, and its result is the security boundary.
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    env=env, timeout=10, preexec_fn=demote)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    env=env, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--clean-incomplete is disabled", result.stderr)
        self.assert_unchanged()

    def test_fresh_empty_directory_passes_readonly_preflight(self):
        self.evidence.unlink()
        result = self.run_readonly_preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dir.exists())

    def test_no_unsafe_cleanup_bypass_remains_in_create_path(self):
        code = REGISTER.read_text()
        self.assertNotIn('CLEAN_INCOMPLETE=1', code)
        self.assertNotIn('rm -rf -- "$RUNNER_DIR"', code[code.index("main() {"):])
        self.assertIn('create_target_preflight "$RUNNER_DIR"', code)
        self.assertLess(code.index('create_target_preflight "$RUNNER_DIR"'),
                        code.index("cli_create_authority stage PRE_REGISTRATION -"))


if __name__ == "__main__":
    unittest.main()
