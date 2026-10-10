#!/usr/bin/env python3
"""Ensure private privileged operations cannot cross a V2 transaction."""
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_binding as binding
from v2_executor_contract import decode_request

CREATE = decode_request(b'{"op":"create","repository":"owner/repo"}')
REMOVE = decode_request(b'{"op":"remove","repository":"owner/repo","runner_name":"runner","runner_id":7,"instance":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')

class BindingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.runner_home = "/home/actions"

    def test_create_pre_registration(self):
        req = {"op":"create_state","repository":"owner/repo",
               "runner_dir":"/home/actions/actions-runner-owner--repo",
               "runner_name":"local-ci-owner--repo","stage":"PRE_REGISTRATION"}
        self.assertEqual(binding.authorize_private_request(self.runtime, CREATE, req), req)

    def test_create_rejects_wrong_repo_or_name(self):
        req = {"op":"create_state","repository":"owner/repo",
               "runner_dir":"/home/actions/actions-runner-owner--repo",
               "runner_name":"local-ci-owner--repo","stage":"PRE_REGISTRATION"}
        for change in ({"repository":"else/repo"}, {"runner_name":"other"},
                       {"runner_dir":"/home/actions/actions-runner-other--repo"},
                       {"stage":"INVALID"}):
            with self.subTest(change=change), self.assertRaises(binding.TransactionBindingError):
                binding.authorize_private_request(self.runtime, CREATE, {**req, **change})

    def test_remove_must_match_instance(self):
        req = {"op":"remove_state","repository":"owner/repo",
               "runner_dir":"/home/actions/actions-runner-owner--repo",
               "runner_name":"runner","stage":"BEGIN"}
        with patch.object(binding, "_current", return_value={"instance":"bb"*16}):
            with self.assertRaises(binding.TransactionBindingError):
                binding.authorize_private_request(self.runtime, REMOVE, req)
        with patch.object(binding, "_current", return_value={"instance":"aa"*16}):
            self.assertEqual(binding.authorize_private_request(self.runtime, REMOVE, req), req)

    def test_cross_transaction_ops_refused(self):
        base = {"repository":"owner/repo", "runner_dir":"/home/actions/actions-runner-owner--repo",
                "runner_name":"runner"}
        for action in ("registration_permission_normalize", "registration_attestation_create",
                       "service_install", "service_start"):
            req = {**base, "op":action}
            if action == "registration_attestation_create":
                req["version"] = "2.328.0"
            if action == "service_start":
                req["service"] = "actions.runner.owner-repo.runner.service"
            with self.subTest(action=action), self.assertRaises(binding.TransactionBindingError):
                binding.authorize_private_request(self.runtime, REMOVE, req)

if __name__ == "__main__":
    unittest.main()
