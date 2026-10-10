#!/usr/bin/env python3
"""Root operation firewall: zero-host tests with a stubbed trusted Runtime."""
import pathlib
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
from v2_privileged import admitted_operation, execute_privileged, PrivilegedProtocolError

BASE = {"op": "service_install", "repository": "owner/repo",
        "runner_dir": "/home/actions/actions-runner-owner--repo", "runner_name": "runner"}

class PrivilegeTests(unittest.TestCase):
    def test_allow_canonical_service_install(self):
        self.assertEqual(admitted_operation(BASE), BASE)

    def test_reject_arbitrary_process_actions(self):
        for op in ("context_check", "service_restart", "exec", "sudo", "recover_local",
                   "delete_dir", "arbitrary_path", "status"):
            with self.subTest(op=op), self.assertRaises(PrivilegedProtocolError):
                admitted_operation({**BASE, "op": op})

    def test_no_extra_fields(self):
        for field in ("shell", "command", "force", "token", "nonce", "uid", "user"):
            with self.subTest(field=field), self.assertRaises(PrivilegedProtocolError):
                admitted_operation({**BASE, field: "malicious"})

    def test_reject_path_traversal(self):
        for path in ("/../../etc", "/home/actions/../etc", "relative",
                     "/home//actions", "/tmp/a/.", "/tmp/a/..", ""):
            with self.subTest(path=path), self.assertRaises(PrivilegedProtocolError):
                admitted_operation({**BASE, "runner_dir": path})

    def test_exact_shared_cli_create_stages(self):
        for stage in ("PRE_REGISTRATION", "REGISTRATION_OUTCOME_UNKNOWN",
                      "REGISTERED_PERMISSION_INCOMPLETE", "REGISTERED_UNIT_INCOMPLETE",
                      "REGISTERED_START_INCOMPLETE", "REGISTERED_HEALTH_UNKNOWN",
                      "CREATE_COMPLETE"):
            with self.subTest(stage=stage):
                value = admitted_operation({**BASE, "op": "create_state", "stage": stage})
                self.assertEqual(value["stage"], stage)

    def test_reject_bad_stages(self):
        for op, stage in (("create_state", "COMPLETE"), ("remove_state", "FORCE"),
                          ("remove_state", "RESET_UNKNOWN")):
            with self.subTest(op=op), self.assertRaises(PrivilegedProtocolError):
                admitted_operation({**BASE, "op": op, "stage": stage})

    def test_service_stop_rejects_unknown_target(self):
        for service in ("../../etc/passwd", "ssh.service", "actions.runner.o/r.service"):
            with self.subTest(service=service), self.assertRaises(PrivilegedProtocolError):
                admitted_operation({**BASE, "op": "service_stop", "service": service})

    def test_delegates_to_single_existing_runtime_authority(self):
        runtime = Mock()
        runtime.privileged.return_value = {"ok": True}
        self.assertEqual(execute_privileged(runtime, BASE, deadline=123.4), {"ok": True})
        runtime.privileged.assert_called_once_with(BASE, deadline=123.4)

    def test_rejects_invalid_runtime_answer(self):
        runtime = Mock()
        runtime.privileged.return_value = {"ok": "yes"}
        with self.assertRaises(PrivilegedProtocolError):
            execute_privileged(runtime, BASE)

if __name__ == "__main__":
    unittest.main()
