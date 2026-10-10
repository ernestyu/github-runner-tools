#!/usr/bin/env python3
"""Mocked V2 Create lifecycle orchestration, no production host changes."""
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_mutation as m
from v2_executor_contract import decode_request


class MutationTests(unittest.TestCase):
    def test_unprivileged_cannot_mutate(self):
        req = decode_request(b'{"op":"create","repository":"owner/repo"}')
        with patch.object(m.os, "geteuid", return_value=1000), \
             patch.object(m, "Runtime") as runtime:
            result = m.execute_mutation(req, b"temporary")
        self.assertEqual(result["error"], "executor_configuration_rejected")
        runtime.assert_not_called()

    def test_remove_guard_rejects_before_cli_spawn(self):
        req = decode_request(b'{"op":"remove","repository":"owner/repo","runner_name":"r","runner_id":7,"instance":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
        from v2_remove_guard import RemoveGuardError
        with patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m, "Runtime"), \
             patch.object(m, "lock_transaction", return_value=123), \
             patch("v2_remove_guard.check_remove_target", side_effect=RemoveGuardError("denied")), \
             patch.object(m.fcntl, "flock"), patch.object(m.os, "close"), \
             patch.object(m, "spawn_cli") as spawn:
            result = m.execute_mutation(req, b"temporary")
        self.assertEqual(result["error"], "remove_identity_rejected")
        spawn.assert_not_called()

    def test_create_busy_rejected_before_spawning(self):
        req = decode_request(b'{"op":"create","repository":"owner/repo"}')
        with patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m, "Runtime"), \
             patch.object(m, "lock_transaction", side_effect=m.MutationError("operation_in_progress")), \
             patch.object(m, "spawn_cli") as spawn:
            result = m.execute_mutation(req, b"temporary")
        self.assertEqual(result["error"], "operation_in_progress")
        spawn.assert_not_called()

    def test_no_arbitrary_config_override(self):
        req = decode_request(b'{"op":"create","repository":"owner/repo"}')
        with patch.object(m.os, "geteuid", return_value=0):
            result = m.execute_mutation(req, b"temporary", config_path="/tmp/fake")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "executor_configuration_rejected")

    def test_privilege_requests_use_existing_runtime(self):
        runtime = MagicMock()
        runtime.privileged.return_value = {"ok": True}
        request = {"op":"service_install", "repository":"owner/repo",
                   "runner_dir":"/home/actions/actions-runner-owner--repo",
                   "runner_name":"runner"}
        from v2_privileged import execute_privileged
        self.assertEqual(execute_privileged(runtime, request), {"ok":True})
        runtime.privileged.assert_called_once()

if __name__ == "__main__":
    unittest.main()
