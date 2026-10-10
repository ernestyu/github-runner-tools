#!/usr/bin/env python3
"""No-root regression tests for fixed V2 CLI launcher admission."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_cli_launcher as launcher


class LauncherTests(unittest.TestCase):
    def test_create_fixed_cli(self):
        self.assertEqual(launcher.resolve_action("create", "ernestyu/github-runner-tools"), (
            "/usr/local/lib/github-runner-tools/web/cli/register-runner.sh",
            "--executor-mode", "--token-fd", "3",
            "--privileged-fd", "4", "--lock-already-held",
            "ernestyu/github-runner-tools"))

    def test_remove_fixed_cli(self):
        argv = launcher.resolve_action("remove", "owner/repo")
        self.assertEqual(argv[0], "/usr/local/lib/github-runner-tools/web/cli/remove-runner.sh")
        self.assertEqual(argv[-1], "owner/repo")
        self.assertNotIn("bash", argv)
        self.assertNotIn("sudo", argv)

    def test_status_no_repo(self):
        self.assertEqual(launcher.resolve_action("status", None),
                         ("/usr/local/lib/github-runner-tools/web/cli/status-runners.sh", "--json"))
        with self.assertRaises(launcher.LaunchError):
            launcher.resolve_action("status", "owner/repo")

    def test_bounded_arguments(self):
        for op, repo in (("recover_local", "owner/repo"), ("exec", "owner/repo"),
                         ("create", "../etc"), ("remove", "o/r;id"),
                         ("create", None), ("remove", "")):
            with self.subTest(op=op, repo=repo), self.assertRaises(launcher.LaunchError):
                launcher.resolve_action(op, repo)

    def test_launch_refuses_nonroot_before_exec(self):
        with patch.object(launcher.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(launcher.LaunchError, "root launcher required"):
                launcher.launch("create", "owner/repo")

    def test_main_rejects_arbitrary_argv(self):
        for argv in (["launcher"], ["launcher", "bash", "-c", "id"],
                     ["launcher", "create", "o/r", "--force"]):
            with patch.object(launcher.sys, "argv", argv):
                self.assertNotEqual(launcher.main(), 0)


if __name__ == "__main__":
    unittest.main()
