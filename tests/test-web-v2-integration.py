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

    def test_v2_never_falls_back_to_v1_mutations(self):
        for op in ("create","remove","recover_local","shell"):
            with self.subTest(op=op), patch.object(client,"fetch_status") as fetch:
                resp = app.dispatch({"V2_EXECUTOR_MODE":"1"},
                                    {"op":op,"repository":"o/r","token":"sample"})
                self.assertFalse(resp["ok"])
                self.assertEqual(resp["error"],"not_implemented")
                fetch.assert_not_called()

    def test_v2_status_error_is_fail_closed(self):
        with patch.object(client,"fetch_status",side_effect=client.ExecutorClientError("down")):
            resp = app.dispatch({"V2_EXECUTOR_MODE":"1"},{"op":"list"})
        self.assertFalse(resp["ok"])

    def test_client_rejects_noncanonical_endpoint(self):
        with self.assertRaises(client.ExecutorClientError):
            client.fetch_status("/tmp/socket")

if __name__ == "__main__":
    unittest.main()
