#!/usr/bin/env python3
"""Single-loop privileged request routing and explicit transaction refusal."""
import json
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
from v2_event_loop import handle_root_request
from v2_executor_contract import decode_request
from v2_binding import TransactionBindingError
from v2_unit_installer import UnitInstallError

class EventLoopTests(unittest.TestCase):
    def test_request_reaches_existing_authority_after_binding(self):
        transaction = decode_request(b'{"op":"create","repository":"o/r"}')
        op = {"op": "create_state", "repository": "o/r",
              "runner_dir": "/home/actions/actions-runner-o--r",
              "runner_name": "local-ci-o--r", "stage": "PRE_REGISTRATION"}
        with patch("v2_event_loop.authorize_private_request", return_value=op) as bind, \
             patch("v2_event_loop.execute_privileged", return_value={"ok": True}) as privileged:
            result = handle_root_request(Mock(), transaction,
                                         json.dumps(op).encode(), 123.0)
        self.assertEqual(json.loads(result), {"ok": True})
        bind.assert_called_once()
        privileged.assert_called_once()

    def test_privilege_binding_rejection_does_not_call_root(self):
        transaction = decode_request(b'{"op":"create","repository":"o/r"}')
        with patch("v2_event_loop.authorize_private_request",
                   side_effect=TransactionBindingError("wrong repo")), \
             patch("v2_event_loop.execute_privileged") as privileged:
            result = handle_root_request(Mock(), transaction, b'{"op":"service_uninstall"}', 123)
        self.assertFalse(json.loads(result)["ok"])
        privileged.assert_not_called()

    def test_service_install_failure_is_contained_at_private_boundary(self):
        transaction = decode_request(b'{"op":"create","repository":"o/r"}')
        op = {"op": "service_install", "repository": "o/r",
              "runner_dir": "/home/actions/actions-runner-o--r",
              "runner_name": "local-ci-o--r", "version": "2.338.0"}
        with patch("v2_event_loop.authorize_private_request", return_value=op), \
             patch("v2_event_loop.execute_privileged",
                   side_effect=UnitInstallError("runner_service_entrypoint_missing")):
            result = handle_root_request(
                Mock(), transaction, json.dumps(op).encode(), 123.0)
        self.assertEqual(
            json.loads(result),
            {"ok": False, "error": "privileged_request_rejected"})

    def test_malformed_json_never_calls_root(self):
        transaction = decode_request(b'{"op":"create","repository":"o/r"}')
        with patch("v2_event_loop.execute_privileged") as privileged:
            result = handle_root_request(Mock(), transaction, b'not json', 123)
        self.assertFalse(json.loads(result)["ok"])
        privileged.assert_not_called()

if __name__ == "__main__":
    unittest.main()
