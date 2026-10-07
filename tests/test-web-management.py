#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))

import grt_web_common as common
import app as web_app
import dispatcher


class CommonTests(unittest.TestCase):
    def test_password_hash_roundtrip(self):
        encoded = common.password_hash("correct horse battery staple", n=2**10)
        self.assertTrue(encoded.startswith("scrypt$"))
        self.assertTrue(common.verify_password("correct horse battery staple", encoded))
        self.assertFalse(common.verify_password("wrong", encoded))

    def test_repository_and_local_identity(self):
        self.assertEqual(common.validate_repository("ernestyu/HyperGrid"), "ernestyu/HyperGrid")
        with self.assertRaises(ValueError):
            common.validate_repository("ernestyu/HyperGrid;rm -rf")
        self.assertEqual(common.make_local_id("ErnestYu", "HyperGrid"), "ernestyu--hypergrid")

    def test_service_name_is_complete_or_fails(self):
        name = common.canonical_service_name("ernestyu/repo", "custom-runner")
        self.assertEqual(name, "actions.runner.ernestyu-repo.custom-runner.service")
        with self.assertRaises(ValueError):
            common.canonical_service_name("owner/" + "r" * 100, "n" * 100)


class SessionAndBrowserTests(unittest.TestCase):
    def setUp(self):
        web_app.SESSIONS.clear()
        web_app.LOGIN_ATTEMPTS.clear()

    def test_session_is_opaque_and_server_side(self):
        app_obj = object.__new__(web_app.App)
        sid, sess = app_obj.new_session()
        self.assertGreaterEqual(len(sid), 32)
        self.assertIn(sid, web_app.SESSIONS)
        self.assertNotIn("authenticated", sid)
        self.assertIn("csrf", sess)
        self.assertEqual(sess["confirm"], {})

    def test_csrf_compare(self):
        sess = {"csrf": "abc"}
        self.assertTrue(web_app.Handler._csrf_ok({"csrf": "abc"}, sess))
        self.assertFalse(web_app.Handler._csrf_ok({"csrf": "def"}, sess))

    def test_cookie_and_cache_security_are_present(self):
        source = (WEB / "app.py").read_text(encoding="utf-8")
        self.assertIn("Secure; HttpOnly; SameSite=Strict", source)
        self.assertIn('Cache-Control", "no-store"', source)
        self.assertIn('bind != "127.0.0.1"', source)


class TokenAdapterTests(unittest.TestCase):
    def test_token_never_enters_child_argv_or_env(self):
        token = "SECRET_WEB_TOKEN_123456"
        with tempfile.TemporaryDirectory() as td:
            child = pathlib.Path(td) / "child.py"
            child.write_text(
                "import os,sys\n"
                "print('Enter token:', flush=True)\n"
                "value=input()\n"
                "print('TOKEN_OK=' + str(value.startswith('SECRET_')), flush=True)\n"
                "print('ARGV_HAS=' + str(any('SECRET_WEB_TOKEN' in x for x in sys.argv)), flush=True)\n"
                "print('ENV_HAS=' + str(any('SECRET_WEB_TOKEN' in v for v in os.environ.values())), flush=True)\n",
                encoding="utf-8",
            )
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, (token + "\n").encode())
                os.close(write_fd)
                write_fd = -1
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(WEB / "pty_token_adapter.py"),
                        "--token-fd",
                        str(read_fd),
                        "--mode",
                        "remove",
                        "--",
                        sys.executable,
                        str(child),
                    ],
                    pass_fds=(read_fd,),
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            finally:
                os.close(read_fd)
                if write_fd >= 0:
                    os.close(write_fd)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn(token, proc.stdout)
            self.assertNotIn(token, proc.stderr)
            self.assertIn("TOKEN_OK=True", proc.stdout)
            self.assertIn("ARGV_HAS=False", proc.stdout)
            self.assertIn("ENV_HAS=False", proc.stdout)


class DispatcherAuthorityTests(unittest.TestCase):
    def runtime(self):
        rt = object.__new__(dispatcher.Runtime)
        rt.runner_user = "actions"
        rt.runner_home = "/home/actions"
        return rt

    @staticmethod
    def regular_root_stat():
        obj = mock.Mock()
        obj.st_mode = stat.S_IFREG | 0o644
        obj.st_uid = 0
        return obj

    def base_props(self):
        return {
            "LoadState": "loaded",
            "FragmentPath": "/etc/systemd/system/actions.runner.owner-repo.runner.service",
            "DropInPaths": "",
            "User": "actions",
            "WorkingDirectory": "/home/actions/actions-runner-owner--repo",
            "ExecStart": "{ path=/home/actions/actions-runner-owner--repo/runsvc.sh ; argv[]=/home/actions/actions-runner-owner--repo/runsvc.sh ; }",
            "ExecStartPre": "",
            "ExecStartPost": "",
            "ExecStop": "",
            "ExecStopPost": "",
            "ExecReload": "",
        }

    def validate_with(self, props):
        rt = self.runtime()
        rt.runner_group = "actions"
        rt._systemctl_show = mock.Mock(return_value=props)
        service = "actions.runner.owner-repo.runner.service"
        unit = (
            "[Service]\n"
            "User=actions\n"
            "WorkingDirectory=/home/actions/actions-runner-owner--repo\n"
            "ExecStart=/home/actions/actions-runner-owner--repo/runsvc.sh\n"
        )
        with mock.patch("dispatcher.os.path.realpath", side_effect=lambda p: p), \
             mock.patch("dispatcher.os.path.islink", return_value=False), \
             mock.patch("dispatcher.os.stat", return_value=self.regular_root_stat()), \
             mock.patch("builtins.open", mock.mock_open(read_data=unit)):
            return rt.validate_unit(
                "owner/repo",
                "/home/actions/actions-runner-owner--repo",
                "runner",
                service,
            )

    def test_canonical_unit_passes(self):
        state, _ = self.validate_with(self.base_props())
        self.assertEqual(state, "present")

    def test_dropin_fails_closed(self):
        props = self.base_props()
        props["DropInPaths"] = "/etc/systemd/system/x.d/evil.conf"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_extra_exec_fails_closed(self):
        props = self.base_props()
        props["ExecStartPre"] = "{ path=/bin/sh ; argv[]=/bin/sh -c evil ; }"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_wrong_user_fails_closed(self):
        props = self.base_props()
        props["User"] = "root"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_wrong_fragment_fails_closed(self):
        props = self.base_props()
        props["FragmentPath"] = "/tmp/unmanaged.service"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_wrong_working_directory_fails_closed(self):
        props = self.base_props()
        props["WorkingDirectory"] = "/home/actions/actions-runner-other--repo"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_wrong_execstart_fails_closed(self):
        props = self.base_props()
        props["ExecStart"] = "{ path=/tmp/evil ; argv[]=/tmp/evil ; }"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_repository_directory_binding_fails_closed(self):
        rt = self.runtime()
        with mock.patch("dispatcher.os.path.realpath", side_effect=lambda p: p), \
             mock.patch("dispatcher.os.path.islink", return_value=False):
            with self.assertRaises(dispatcher.DispatchError):
                rt.validate_runner_dir(
                    "owner/repo",
                    "/home/actions/actions-runner-someone--else",
                )

    def test_public_schema_has_no_command_path_fields(self):
        forbidden = {"exec", "shell", "command", "path", "script", "argv", "uid", "gid", "service_name", "systemd_unit"}
        for fields in dispatcher.ALLOWED_PUBLIC.values():
            self.assertTrue(forbidden.isdisjoint(fields))

    def test_worker_hardens_same_uid_fd_boundary(self):
        source = (WEB / "lifecycle_worker.py").read_text(encoding="utf-8")
        self.assertIn("PR_SET_DUMPABLE", source)
        self.assertIn("disable_ptrace_dumpability()", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
