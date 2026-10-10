#!/usr/bin/env python3
"""Canonical Unit generation accepted by the shared CLI validation authority."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
from v2_unit_installer import unit_bytes, install_canonical_unit, UnitInstallError
from cli_create_authority import validate_official_unit


class UnitTests(unittest.TestCase):
    def test_canonical_template_matches_interactive_cli_validator(self):
        content = unit_bytes("owner/repo", "runner",
                             "/home/actions/actions-runner-owner--repo", "actions")
        self.assertNotIn(b"ExecStartPre=", content)
        self.assertNotIn(b"sudo", content)
        validate_official_unit(
            content.decode().splitlines(), "actions",
            "/home/actions/actions-runner-owner--repo",
        )

    def test_unit_is_fixed_and_no_extra_exec_surfaces(self):
        content = unit_bytes("owner/repo", "runner", "/safe/runner", "actions").decode()
        self.assertEqual(content.count("ExecStart="), 1)
        self.assertNotIn("ExecStop=", content)
        self.assertEqual(content.count("Type=simple"), 1)
        self.assertEqual(content.count("User=actions"), 1)

    def test_nonroot_install_is_rejected(self):
        with patch("v2_unit_installer.os.geteuid", return_value=1000):
            with self.assertRaises(UnitInstallError):
                install_canonical_unit("owner/repo", "runner", "/safe/runner", "actions")

    def test_invalid_runner_user_and_directory_rejected(self):
        for user, path in (("evil\nUser=root", "/safe/runner"),
                           ("actions", "relative/path"),
                           ("actions", "/safe\n[Service]")):
            with self.subTest(user=user, path=path), self.assertRaises(UnitInstallError):
                unit_bytes("owner/repo", "runner", path, user)


if __name__ == "__main__":
    unittest.main()
