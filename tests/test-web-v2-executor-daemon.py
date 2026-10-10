#!/usr/bin/env python3
"""Exercise V2 daemon request handling without root, Runner or host writes."""
import json
import pathlib
import socket
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_executor
from v2_executor_contract import decode_request
from v2_executor_transport import encode_request_frame, ExecutorError


class ExecutorDaemonTests(unittest.TestCase):
    def request(self, frame, handler=None, authorize=True):
        left, right = socket.socketpair()
        output = []
        def worker():
            with right:
                with patch.object(v2_executor, "admit_peer",
                                  side_effect=None if authorize else ExecutorError("unauthorized")):
                    v2_executor.service_one(right, execute_fn=handler or v2_executor.execute)
        t = threading.Thread(target=worker)
        t.start()
        try:
            with left:
                try:
                    left.sendall(frame)
                except BrokenPipeError:
                    if not authorize:
                        return {"ok": False, "error": "rejected"}
                    raise
                size = int.from_bytes(left.recv(4), "big")
                self.assertLess(size, 1048577)
                while len(output) < size:
                    block = left.recv(size - len(output))
                    if not block:
                        break
                    output.extend(block)
                return json.loads(bytes(output))
        finally:
            t.join(timeout=3)
            self.assertFalse(t.is_alive())

    def test_status_invokes_cli_component(self):
        status = decode_request(b'{"op":"status"}')
        with patch.object(v2_executor, "status_as_runner",
                          return_value={"ok":True,"data":{"runners":[]}}) as cli:
            result = self.request(encode_request_frame(status, None))
        self.assertEqual(result, {"ok":True,"data":{"runners":[]}})
        cli.assert_called_once_with()

    def test_create_forwards_only_to_restricted_mutation(self):
        request = decode_request(b'{"op":"create","repository":"owner/repo"}')
        with patch("v2_mutation.execute_mutation",
                   return_value={"ok": False, "error": "operation_in_progress"}) as run:
            result = self.request(encode_request_frame(request, b"temporary"))
        self.assertEqual(result["error"], "operation_in_progress")
        self.assertEqual(run.call_args.args[0], request)
        self.assertEqual(run.call_args.args[1], b"temporary")

    def test_remove_fails_closed_until_attested(self):
        request = decode_request(b'{"op":"remove","repository":"owner/repo","runner_name":"r","runner_id":7,"instance":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
        result = self.request(encode_request_frame(request, b"temporary"))
        self.assertEqual(result["error"], "remove_attestation_not_wired")

    def test_peer_denied_before_executor(self):
        status = decode_request(b'{"op":"status"}')
        result = self.request(encode_request_frame(status, None), authorize=False)
        self.assertEqual(result["error"], "rejected")

    def test_invalid_json_rejected(self):
        result = self.request((2).to_bytes(4,"big") + b'{}' + b'\x00\x00')
        self.assertEqual(result["error"], "rejected")

    def test_no_arbitrary_socket_path(self):
        with self.assertRaises(ExecutorError):
            v2_executor.serve("/tmp/unauthorized-executor.sock")


if __name__ == "__main__":
    unittest.main()
