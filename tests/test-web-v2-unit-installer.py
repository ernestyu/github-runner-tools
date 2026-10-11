#!/usr/bin/env python3
"""Canonical Unit and official service-wrapper bootstrap contracts."""
import hashlib
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_unit_installer as m
import cli_create_authority as cli_auth
from cli_create_authority import validate_official_unit


class UnitTests(unittest.TestCase):
    def test_canonical_template_matches_interactive_cli_validator(self):
        content = m.unit_bytes("owner/repo", "runner",
                               "/home/actions/actions-runner-owner--repo", "actions")
        self.assertNotIn(b"ExecStartPre=", content)
        self.assertNotIn(b"sudo", content)
        validate_official_unit(
            content.decode().splitlines(), "actions",
            "/home/actions/actions-runner-owner--repo",
        )

    def test_unit_is_fixed_and_no_extra_exec_surfaces(self):
        content = m.unit_bytes("owner/repo", "runner", "/safe/runner", "actions").decode()
        self.assertEqual(content.count("ExecStart="), 1)
        self.assertNotIn("ExecStop=", content)
        self.assertEqual(content.count("Type=simple"), 1)
        self.assertEqual(content.count("User=actions"), 1)

    def test_nonroot_install_is_rejected(self):
        with patch.object(m.os, "geteuid", return_value=1000):
            with self.assertRaises(m.UnitInstallError) as ctx:
                m.install_canonical_unit("owner/repo", "runner", "/safe/runner", "actions")
        self.assertEqual(ctx.exception.reason, "authority_install_failed")

    def test_invalid_runner_user_and_directory_rejected(self):
        for user, path in (("evil\nUser=root", "/safe/runner"),
                           ("actions", "relative/path"),
                           ("actions", "/safe\n[Service]")):
            with self.subTest(user=user, path=path), self.assertRaises(m.UnitInstallError):
                m.unit_bytes("owner/repo", "runner", path, user)

    def make_runner(self, root: pathlib.Path, wrapper=True):
        runner = root / "actions-runner-owner--repo"
        (runner / "bin").mkdir(parents=True)
        if wrapper:
            src = runner / "bin/runsvc.sh"
            src.write_bytes(b"#!/bin/bash\necho official-wrapper\n")
            src.chmod(0o755)
        return runner

    def account(self):
        return SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())

    def test_fresh_post_config_bootstrap_requires_archive_wrapper(self):
        with tempfile.TemporaryDirectory() as td:
            runner = self.make_runner(pathlib.Path(td), wrapper=False)
            self.assertFalse((runner / "runsvc.sh").exists())
            with patch.object(m.pwd, "getpwnam", return_value=self.account()):
                with self.assertRaises(m.UnitInstallError) as ctx:
                    m.bootstrap_service_entrypoint(str(runner), "actions")
            self.assertEqual(ctx.exception.reason, "runner_service_entrypoint_missing")
            self.assertFalse((runner / "runsvc.sh").exists())

    def test_fresh_post_config_bootstrap_copies_exact_official_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            runner = self.make_runner(pathlib.Path(td))
            source = runner / "bin/runsvc.sh"
            self.assertFalse((runner / "runsvc.sh").exists())
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            with patch.object(m.pwd, "getpwnam", return_value=self.account()):
                actual = m.bootstrap_service_entrypoint(str(runner), "actions")
            dest = runner / "runsvc.sh"
            self.assertEqual(actual, expected)
            self.assertEqual(dest.read_bytes(), source.read_bytes())
            self.assertEqual(dest.stat().st_mode & 0o777, 0o755)
            self.assertEqual(dest.stat().st_uid, os.getuid())
            self.assertEqual(dest.stat().st_gid, os.getgid())

    def test_bootstrap_refuses_symlink_source_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            runner = self.make_runner(base, wrapper=False)
            target = runner / "real"
            target.write_text("x")
            (runner / "bin/runsvc.sh").symlink_to(target)
            with patch.object(m.pwd, "getpwnam", return_value=self.account()):
                with self.assertRaises(m.UnitInstallError) as ctx:
                    m.bootstrap_service_entrypoint(str(runner), "actions")
            self.assertEqual(ctx.exception.reason, "runner_service_entrypoint_invalid")

        with tempfile.TemporaryDirectory() as td:
            runner = self.make_runner(pathlib.Path(td))
            (runner / "runsvc.sh").write_text("preexisting")
            with patch.object(m.pwd, "getpwnam", return_value=self.account()):
                with self.assertRaises(m.UnitInstallError) as ctx:
                    m.bootstrap_service_entrypoint(str(runner), "actions")
            self.assertEqual(ctx.exception.reason, "runner_service_entrypoint_invalid")
            self.assertEqual((runner / "runsvc.sh").read_text(), "preexisting")


    def test_trusted_authority_reason_exit_mapping_is_fixed(self):
        expected = {
            "runner_service_entrypoint_missing": 64,
            "runner_service_entrypoint_invalid": 65,
            "unit_target_exists": 66,
            "unit_stage_create_failed": 67,
            "unit_publish_failed": 68,
            "unit_daemon_reload_failed": 69,
            "unit_enable_failed": 70,
            "authority_state_rejected": 71,
            "authority_identity_rejected": 72,
            "authority_install_failed": 73,
        }
        self.assertEqual(cli_auth.EXIT_BY_REASON, expected)
        for reason, code in expected.items():
            with self.subTest(reason=reason):
                exc = m.UnitInstallError(reason)
                self.assertEqual(cli_auth._safe_reason(exc), reason)
                self.assertEqual(cli_auth.EXIT_BY_REASON[cli_auth._safe_reason(exc)], code)
        self.assertEqual(
            cli_auth._safe_reason(m.UnitInstallError("arbitrary-untrusted-detail")),
            "authority_install_failed",
        )



if __name__ == "__main__":
    unittest.main()
