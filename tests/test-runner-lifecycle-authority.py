#!/usr/bin/env python3
"""Run with root to test the actual root-only journal/attestation authority.

The entire authority root is redirected to a fresh private /tmp sandbox.
No production data, systemd Unit, GitHub API or Runner is touched.
"""
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
import runner_lifecycle_authority as auth

REPO = "example/repo"
RUNNER = "fixture"


@unittest.skipUnless(os.geteuid() == 0, "root-only isolation fixture")
class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.base = Path(self.sandbox.name)
        self.previous = auth.ROOT
        auth.ROOT = str(self.base / "state")
        self.addCleanup(setattr, auth, "ROOT", self.previous)
        self.runner = self.base / "actions-runner-example--repo"
        self.runner.mkdir()
        self.repo = str(self.runner)
        (self.runner / ".runner").write_text(json.dumps({
            "agentName": RUNNER, "gitHubUrl": "https://github.com/example/repo"}))
        (self.runner / ".credentials").write_text("SYNTHETIC_NOT_SECRET")
        for name in (".runner", ".credentials"):
            (self.runner / name).chmod(0o600)

    def test_full_stage_transitions_and_restart_block(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_UNIT_INCOMPLETE")
        for stage in auth.STAGES[1:]:
            if stage == "REGISTERED_UNIT_INCOMPLETE":
                auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
            auth.create_stage(REPO, RUNNER, self.repo, stage)
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        ledger = list((self.base / "state" / "create-state").glob("*.json"))
        self.assertEqual(len(ledger), 1)
        self.assertEqual(json.loads(ledger[0].read_text())["stage"], "CREATE_COMPLETE")
        self.assertEqual(ledger[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn("SYNTHETIC_NOT_SECRET", ledger[0].read_text())

    def test_uncertain_registration_is_terminal(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTRATION_OUTCOME_UNKNOWN")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")

    def test_root_only_attestation_contains_digests_not_credentials(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        auth.create_attestation(REPO, RUNNER, self.repo, pwd.getpwuid(os.getuid()).pw_name, "2.328.0")
        ledger = list((self.base / "state" / "runner-attestations").glob("*.json"))
        self.assertEqual(len(ledger), 1)
        payload = ledger[0].read_text()
        self.assertNotIn("SYNTHETIC_NOT_SECRET", payload)
        entry = json.loads(payload)
        self.assertEqual(entry["repository"], REPO)
        self.assertEqual(entry["runner"], RUNNER)
        self.assertEqual(entry["capture_stage"], "registered-secure-before-service")
        self.assertEqual(set(entry["files"]), {".runner", ".credentials"})
        self.assertEqual(ledger[0].stat().st_mode & 0o777, 0o600)
        with self.assertRaises(auth.AuthorityError):
            auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")

    def test_attestation_rejects_insecure_or_symlinked_files(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        credentials = self.runner / ".credentials"
        credentials.chmod(0o664)
        with self.assertRaises(auth.AuthorityError):
            auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
        credentials.chmod(0o600)
        credentials.unlink()
        credentials.symlink_to(self.runner / ".runner")
        with self.assertRaises((auth.AuthorityError, OSError)):
            auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
        self.assertFalse((self.base / "state" / "runner-attestations").exists()
                         and list((self.base / "state" / "runner-attestations").glob("*.json")))

    def test_attestation_wrong_repository_rejected(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        with self.assertRaises(auth.AuthorityError):
            auth.create_attestation("foreign/repo", RUNNER, self.repo, "root", "2.328.0")


if __name__ == "__main__":
    unittest.main()
