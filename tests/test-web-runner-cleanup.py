#!/usr/bin/env python3
"""FD-relative cleanup never touches sibling data, symlink targets or mounts."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("scoped_runner_cleanup", ROOT / "scripts/web-runner-cleanup.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ScopedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.runner = self.base / "actions-runner-example--repo"
        self.runner.mkdir()
        self.sibling = self.base / "actions-runner-another--repo"
        self.sibling.mkdir()
        (self.sibling / "protected").write_text("SIBLING")
        self.archive = self.base / "external-archive"
        self.archive.mkdir()
        (self.archive / "valuable").write_text("DO-NOT-DELETE")
        self.dir = self.runner / "bin"
        self.dir.mkdir()
        (self.dir / "runsvc.sh").write_text("#!/bin/sh\n")
        (self.runner / ".runner").write_text("metadata")
        (self.runner / ".service").write_text("service")

    def remove(self):
        st = self.runner.stat()
        module.cleanup(str(self.base), str(self.runner), "example/repo", st.st_dev, st.st_ino)

    def verify_others(self):
        self.assertEqual((self.sibling / "protected").read_text(), "SIBLING")
        self.assertEqual((self.archive / "valuable").read_text(), "DO-NOT-DELETE")

    def test_nested_tree_symlink_never_follows_target(self):
        (self.runner / "sibling-link").symlink_to(self.sibling, target_is_directory=True)
        (self.dir / "archive-link").symlink_to(self.archive, target_is_directory=True)
        self.remove()
        self.assertFalse(self.runner.exists())
        self.verify_others()

    def test_hardlink_outside_target_rejected_before_deletion(self):
        os.link(self.sibling / "protected", self.runner / "linked-file")
        with self.assertRaises(module.CleanupRejected):
            self.remove()
        self.assertTrue(self.runner.exists())
        self.assertEqual((self.runner / ".runner").read_text(), "metadata")
        self.verify_others()

    def test_wrong_directory_inode_rejected(self):
        st = self.runner.stat()
        with self.assertRaises(module.CleanupRejected):
            module.cleanup(str(self.base), str(self.runner), "example/repo", st.st_dev, st.st_ino + 1)
        self.assertTrue(self.runner.exists())
        self.verify_others()

    def test_wrong_repository_and_symlinked_target_rejected(self):
        st = self.runner.stat()
        with self.assertRaises(module.CleanupRejected):
            module.cleanup(str(self.base), str(self.runner), "wrong/repo", st.st_dev, st.st_ino)
        link = self.base / "actions-runner-wrong--repo"
        link.symlink_to(self.runner, target_is_directory=True)
        with self.assertRaises((module.CleanupRejected, OSError)):
            module.cleanup(str(self.base), str(link), "wrong/repo", st.st_dev, st.st_ino)
        self.verify_others()

    def test_same_device_bind_mount_id_change_rejected(self):
        native_mount_id = module._mount_id
        target_inode = self.dir.stat().st_ino
        def mocked(fd):
            value = native_mount_id(fd)
            return value + 1 if os.fstat(fd).st_ino == target_inode else value
        with mock.patch.object(module, "_mount_id", side_effect=mocked):
            with self.assertRaises(module.CleanupRejected):
                self.remove()
        self.assertTrue(self.runner.exists())
        self.verify_others()

    def test_swap_of_top_level_directory_detected_without_foreign_deletion(self):
        original = module._walk
        moved = self.base / "moved-runner"
        swapped = False
        def race(fd, mount, dev, mutate):
            nonlocal swapped
            result = original(fd, mount, dev, mutate)
            if not mutate and not swapped:
                self.runner.rename(moved)
                self.runner.mkdir()
                (self.runner / "new-file").write_text("REPLACEMENT")
                swapped = True
            return result
        with mock.patch.object(module, "_walk", side_effect=race):
            with self.assertRaises(module.CleanupRejected):
                self.remove()
        self.assertEqual((self.runner / "new-file").read_text(), "REPLACEMENT")
        self.assertEqual((moved / ".runner").read_text(), "metadata")
        self.verify_others()


if __name__ == "__main__":
    unittest.main()
