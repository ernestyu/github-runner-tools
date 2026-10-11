#!/usr/bin/env python3
"""Pinned root-owned V2 executable path provenance, without host mutation."""
import pathlib
import stat
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_code_provenance as p

class ProvenanceTests(unittest.TestCase):
    def test_enumerated_paths_only(self):
        for path in ("/tmp/script.sh", "/home/actions/runner", 
                     "/usr/local/lib/github-runner-tools/cli/../cli/remove-runner.sh"):
            with self.subTest(path=path), self.assertRaises(p.CodeProvenanceError):
                p.verify_fixed_file(path)

    def test_root_regular_one_link_and_parent(self):
        regular = SimpleNamespace(st_mode=stat.S_IFREG | 0o755,st_uid=0,st_nlink=1)
        folder = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755,st_uid=0)
        def fake_lstat(path):
            return regular if path.endswith("register-runner.sh") else folder
        with patch.object(p.os, "lstat", side_effect=fake_lstat):
            p.verify_fixed_file("/usr/local/lib/github-runner-tools/cli/register-runner.sh")

    def test_reject_mutable_parent_and_symlink_target(self):
        regular = SimpleNamespace(st_mode=stat.S_IFREG | 0o755,st_uid=0,st_nlink=1)
        folder = SimpleNamespace(st_mode=stat.S_IFDIR | 0o775,st_uid=0)
        with patch.object(p.os, "lstat", return_value=folder):
            with self.assertRaises(p.CodeProvenanceError):
                p.verify_fixed_file("/usr/local/lib/github-runner-tools/cli/register-runner.sh")
        safe_dir = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755,st_uid=0)
        link = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777,st_uid=0,st_nlink=1)
        def fake_link(path):
            return link if path.endswith("register-runner.sh") else safe_dir
        with patch.object(p.os, "lstat", side_effect=fake_link):
            with self.assertRaises(p.CodeProvenanceError):
                p.verify_fixed_file("/usr/local/lib/github-runner-tools/cli/register-runner.sh")

    def test_reject_hardlinked_pinned_cli(self):
        folder = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755,st_uid=0)
        hardlink = SimpleNamespace(st_mode=stat.S_IFREG | 0o755,st_uid=0,st_nlink=2)
        def fake_lstat(path):
            return hardlink if path.endswith("remove-runner.sh") else folder
        with patch.object(p.os, "lstat", side_effect=fake_lstat):
            with self.assertRaises(p.CodeProvenanceError):
                p.verify_fixed_file("/usr/local/lib/github-runner-tools/cli/remove-runner.sh")

if __name__ == "__main__":
    unittest.main()
