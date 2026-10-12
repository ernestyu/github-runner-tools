#!/usr/bin/env python3
"""Isolated service-record security tests; no systemd or GitHub mutation."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/web-service-record.py"
SPEC = importlib.util.spec_from_file_location("web_service_record", HELPER)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
REPO = "example/repo"
RUNNER = "fixture"
UNIT = "actions.runner.example-repo.fixture.service"


class ServiceRecordSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runner = self.base / "actions-runner-example--repo"
        self.runner.mkdir()
        self.sibling = self.base / "actions-runner-other--repo"
        self.sibling.mkdir()
        (self.sibling / ".service").write_text("SIBLING-DO-NOT-TOUCH")
        self.metadata = self.runner / ".runner"
        self.metadata.write_text(json.dumps({"agentName": RUNNER, "gitHubUrl": "https://github.com/example/repo"}))
        self.metadata.chmod(0o600)
        self.credentials = self.runner / ".credentials"
        self.credentials.write_text("SYNTHETIC-CREDENTIAL-NOT-REAL")
        self.credentials.chmod(0o600)
        self.record = self.runner / ".service"
        self.record.write_text(UNIT + "\n")

    def call(self, mode, proof=""):
        return module.run(mode, str(self.base), str(self.runner), REPO, RUNNER, UNIT, proof)

    def quarantines(self):
        return list(self.runner.glob(".grt-service-reconcile-*"))

    def test_successful_atomic_quarantine_preserves_registration_assets_and_other_runner(self):
        self.call("check")
        # Capture exact inode before isolation.
        inode = self.record.stat().st_ino
        with contextlib.redirect_stdout(io.StringIO()):
            self.call("quarantine")
        q = self.quarantines()
        self.assertEqual(len(q), 1)
        self.assertFalse(self.record.exists())
        self.assertEqual(q[0].stat().st_ino, inode)
        self.assertEqual(q[0].read_text(), UNIT + "\n")
        self.assertEqual(self.credentials.read_text(), "SYNTHETIC-CREDENTIAL-NOT-REAL")
        self.assertTrue(self.metadata.is_file())
        self.assertEqual((self.sibling / ".service").read_text(), "SIBLING-DO-NOT-TOUCH")
        self.assertRaises(ValueError, self.call, "quarantine")
        self.assertRaises(ValueError, self.call, "check")

    def test_quarantine_proof_is_inode_pinned(self):
        # Capture the helper's proof in a subprocess, as Web Remove does.
        proc = subprocess.run([sys.executable, str(HELPER), "quarantine",
            str(self.base), str(self.runner), REPO, RUNNER, UNIT],
            capture_output=True, text=True, check=True)
        proof = proc.stdout.strip()
        self.assertRegex(proof, r"^\.grt-service-reconcile-[0-9a-f]{32}:\d+:\d+:\d+$")
        self.call("verify", proof)
        quarantined = self.quarantines()[0]
        quarantined.unlink()  # Adversarial same-UID replacement, not implementation mutation.
        quarantined.write_text(UNIT + "\n")
        with self.assertRaises(ValueError):
            self.call("verify", proof)
        self.assertEqual(quarantined.read_text(), UNIT + "\n")

    def test_missing_unsafe_mismatched_or_ambiguous_record_fails_closed(self):
        for kind in ("missing", "symlink", "hardlink", "mismatch", "world_writable", "directory", "stale"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as scratch:
                    original = self.record.read_bytes()
                    if kind == "missing":
                        self.record.unlink()
                    elif kind == "symlink":
                        self.record.unlink()
                        self.record.symlink_to(self.sibling / ".service")
                    elif kind == "hardlink":
                        os.link(self.record, Path(scratch) / "link")
                    elif kind == "mismatch":
                        self.record.write_text("actions.runner.foreign-repo.fixture.service\n")
                    elif kind == "world_writable":
                        self.record.chmod(0o666)
                    elif kind == "directory":
                        self.record.unlink()
                        self.record.mkdir()
                    elif kind == "stale":
                        (self.runner / ".grt-service-reconcile-deadbeef").write_text("stale")
                    try:
                        with self.assertRaises((OSError, ValueError)):
                            self.call("check")
                    finally:
                        if self.record.is_symlink() or self.record.is_file():
                            self.record.unlink()
                        elif self.record.is_dir():
                            self.record.rmdir()
                        self.record.write_bytes(original)
                        self.record.chmod(0o644)
                        stale = self.runner / ".grt-service-reconcile-deadbeef"
                        if stale.exists():
                            stale.unlink()
                    self.assertEqual((self.sibling / ".service").read_text(), "SIBLING-DO-NOT-TOUCH")

    def test_directory_symlink_and_repo_mismatch_rejected(self):
        other = self.base / "actions-runner-not-this-repo"
        other.symlink_to(self.runner, target_is_directory=True)
        for target, repo in ((other, REPO), (self.runner, "elsewhere/repo")):
            with self.subTest(target=target, repo=repo):
                with self.assertRaises((OSError, ValueError)):
                    module.run("quarantine", str(self.base), str(target), repo, RUNNER, UNIT)
        self.assertTrue(self.record.exists())

    def test_source_swapped_between_validation_and_atomic_move_never_unlinked(self):
        original_rename = module.rename_noreplace
        raced = self.runner / "raced-file"
        raced.write_text("UNRELATED-SAME-UID-CONTENT")
        def swap(fd, source, dest):
            if source == ".service":
                os.rename(".service", "original-preserved", src_dir_fd=fd, dst_dir_fd=fd)
                os.rename("raced-file", ".service", src_dir_fd=fd, dst_dir_fd=fd)
            return original_rename(fd, source, dest)
        with mock.patch.object(module, "rename_noreplace", side_effect=swap):
            with self.assertRaises(ValueError):
                self.call("quarantine")
        self.assertEqual((self.runner / "original-preserved").read_text(), UNIT + "\n")
        self.assertEqual(self.record.read_text(), "UNRELATED-SAME-UID-CONTENT")
        self.assertEqual((self.sibling / ".service").read_text(), "SIBLING-DO-NOT-TOUCH")
        self.assertTrue(self.metadata.is_file())
        self.assertTrue(self.credentials.is_file())

    def test_renameat2_unavailable_fails_without_deleting_record(self):
        with mock.patch.object(module, "rename_noreplace", side_effect=OSError(38, "unsupported")):
            with self.assertRaises(OSError):
                self.call("quarantine")
        self.assertEqual(self.record.read_text(), UNIT + "\n")
        self.assertEqual(self.quarantines(), [])
        self.assertEqual(self.credentials.read_text(), "SYNTHETIC-CREDENTIAL-NOT-REAL")
        self.assertEqual((self.sibling / ".service").read_text(), "SIBLING-DO-NOT-TOUCH")

    def test_racing_source_disappearance_fails_without_cross_runner_effect(self):
        original_rename = module.rename_noreplace
        def remove_before_move(fd, source, dest):
            if source == ".service":
                os.rename(".service", "service-temporary", src_dir_fd=fd, dst_dir_fd=fd)
            return original_rename(fd, source, dest)
        with mock.patch.object(module, "rename_noreplace", side_effect=remove_before_move):
            with self.assertRaises(FileNotFoundError):
                self.call("quarantine")
        self.assertEqual((self.runner / "service-temporary").read_text(), UNIT + "\n")
        self.assertEqual((self.sibling / ".service").read_text(), "SIBLING-DO-NOT-TOUCH")
        self.assertTrue(self.metadata.exists())
        self.assertTrue(self.credentials.exists())

    def test_post_move_replacement_never_unlinks_foreign_record(self):
        original_record = module.record
        def replace_after_move(fd, basename, uid, expected):
            if basename.startswith(module.PREFIX):
                os.rename(basename, "approved-original", src_dir_fd=fd, dst_dir_fd=fd)
                with os.fdopen(os.open(basename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd), "wb") as out:
                    out.write(b"FOREIGN-DATA")
            return original_record(fd, basename, uid, expected)
        with mock.patch.object(module, "record", side_effect=replace_after_move):
            with self.assertRaises(ValueError):
                self.call("quarantine")
        self.assertEqual((self.runner / "approved-original").read_text(), UNIT + "\n")
        self.assertEqual((self.sibling / ".service").read_text(), "SIBLING-DO-NOT-TOUCH")
        self.assertTrue(self.metadata.is_file())
        self.assertTrue(self.credentials.is_file())


if __name__ == "__main__":
    unittest.main()
