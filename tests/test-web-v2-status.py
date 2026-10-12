#!/usr/bin/env python3
"""No-host tests of the configured Runner Status execution boundary."""
import json
import pathlib
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_status


class StatusBoundaryTests(unittest.TestCase):
    def test_requires_root(self):
        with patch.object(v2_status.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(v2_status.StatusError, "executor must be root"):
                v2_status.status_as_runner()

    def test_root_identity_is_rejected_but_arbitrary_nonroot_owner_is_supported(self):
        with patch.object(v2_status.pwd, "getpwnam",
                          return_value=SimpleNamespace(pw_uid=0, pw_gid=0, pw_dir="/root")):
            with self.assertRaisesRegex(v2_status.StatusError, "invalid runner identity"):
                v2_status.runner_identity("root")
        with patch.object(v2_status.pwd, "getpwnam",
                          return_value=SimpleNamespace(pw_uid=1234, pw_gid=1234,
                                                       pw_dir="/srv/runner-user")):
            self.assertEqual(
                v2_status.runner_identity("runner-user"),
                (1234, 1234, "/srv/runner-user"))

    def test_status_uses_cli_without_shell_or_token(self):
        result = subprocess.CompletedProcess([], 0, stdout=b'{"runners":[]}', stderr=b"")
        with patch.object(v2_status.os, "geteuid", return_value=0), \
             patch.object(v2_status, "configured_runner_user", return_value="runner-user"), \
             patch.object(v2_status, "runner_identity", return_value=(1000, 1000, "/home/runner-user")), \
             patch("v2_code_provenance.verify_launch") as verify, \
             patch.object(v2_status.subprocess, "run", return_value=result) as run:
            self.assertEqual(v2_status.status_as_runner()["data"], {"runners": []})
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/local/lib/github-runner-tools/cli/status-runners.sh", "--json"])
        self.assertEqual(kwargs["user"], 1000)
        self.assertEqual(kwargs["group"], 1000)
        self.assertEqual(kwargs["extra_groups"], [])
        self.assertEqual(kwargs["env"]["USER"], "runner-user")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("token", json.dumps(kwargs))

    def test_rejects_invalid_or_failed_cli(self):
        for result in (
            subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"token secret"),
            subprocess.CompletedProcess([], 0, stdout=b"not json", stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=b"0", stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=b"x" * 1048577, stderr=b""),
        ):
            with self.subTest(code=result.returncode, size=len(result.stdout)), \
                 patch.object(v2_status.os, "geteuid", return_value=0), \
                 patch.object(v2_status, "configured_runner_user", return_value="runner-user"), \
                 patch.object(v2_status, "runner_identity", return_value=(1000, 1000, "/home/runner-user")), \
                 patch("v2_code_provenance.verify_launch"), \
                 patch.object(v2_status.subprocess, "run", return_value=result), \
                 self.assertRaises(v2_status.StatusError) as caught:
                v2_status.status_as_runner()
            self.assertNotIn("token", str(caught.exception))

    def test_timeout_is_bounded(self):
        with patch.object(v2_status.os, "geteuid", return_value=0):
            for value in (0, 61, True):
                with self.subTest(value=value), self.assertRaises(v2_status.StatusError):
                    v2_status.status_as_runner(timeout=value)


if __name__ == "__main__":
    unittest.main()
