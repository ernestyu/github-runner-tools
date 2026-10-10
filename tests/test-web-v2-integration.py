#!/usr/bin/env python3
"""V2 Web Status integration regression without Debian or Runner changes."""
import json
import pathlib
import socket
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import app
import v2_executor_client as client


class WebV2StatusTests(unittest.TestCase):
    def test_v2_web_status_reads_cli_inventory(self):
        example = [
            {"repository":"o/r","runner_name":"runner","service_state":"active",
             "configured":True,"runner_dir":"/home/actions/actions-runner-o--r",
             "can_remove":True,"can_recover_local":True}
        ]
        with patch.object(client, "fetch_status", return_value={"ok":True,"data":example}):
            response = app.dispatch({"V2_EXECUTOR_MODE":"1"}, {"op":"list"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["runners"][0]["repository"], "o/r")
        self.assertFalse(response["runners"][0]["can_remove"])
        self.assertFalse(response["runners"][0]["can_recover_local"])

    def test_v2_create_uses_only_executor_client(self):
        with patch.object(client, "create_runner",
                          return_value={"ok": False, "error": "operation_in_progress"}) as create:
            resp = app.dispatch({"V2_EXECUTOR_MODE": "1"},
                                {"op": "create", "repository": "o/r", "token": "sample"})
        self.assertEqual(resp["error"], "operation_in_progress")
        create.assert_called_once_with("o/r", "sample")

    def test_v2_remove_uses_attested_executor_contract(self):
        with patch.object(client, "remove_runner",
                          return_value={"ok": False, "error": "remove_identity_rejected"}) as remove:
            result = app.dispatch({"V2_EXECUTOR_MODE": "1"}, {
                "op": "remove", "repository": "o/r", "runner_name": "runner",
                "runner_id": 7, "instance": "aa" * 16, "token": "sample"})
        self.assertEqual(result["error"], "remove_identity_rejected")
        remove.assert_called_once_with("o/r", "runner", 7, "aa" * 16, "sample")

    def test_v2_never_falls_back_to_legacy_recovery(self):
        for op in ("recover_local", "shell"):
            with self.subTest(op=op), patch.object(client, "fetch_status") as fetch:
                resp = app.dispatch({"V2_EXECUTOR_MODE": "1"},
                                    {"op": op, "repository": "o/r", "token": "sample"})
                self.assertFalse(resp["ok"])
                self.assertEqual(resp["error"], "not_implemented")
                fetch.assert_not_called()

    def test_failure_message_never_reflects_untrusted_token_or_stage(self):
        result = {
            "ok": False, "error": "raw-secret",
            "stage": "<script>stolen-token</script>",
            "exit_code": 88, "correlation_id": "ab" * 6,
            "token": "raw-secret",
        }
        safe = app.v2_failure_text(result, "Create")
        self.assertIn("CLI exit: 88", safe)
        self.assertIn("Request ID:", safe)
        self.assertNotIn("raw-secret", safe)
        self.assertNotIn("stolen-token", safe)
        self.assertIn("before retrying", safe)

    def test_v2_status_error_is_fail_closed(self):
        with patch.object(client,"fetch_status",side_effect=client.ExecutorClientError("down")):
            resp = app.dispatch({"V2_EXECUTOR_MODE":"1"},{"op":"list"})
        self.assertFalse(resp["ok"])

    def test_client_rejects_noncanonical_endpoint(self):
        with self.assertRaises(client.ExecutorClientError):
            client.fetch_status("/tmp/socket")

if __name__ == "__main__":
    unittest.main()
