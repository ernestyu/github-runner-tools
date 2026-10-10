#!/usr/bin/env python3
"""Real child/socket end-to-end test of one bounded V2 privileged exchange.

No root operations, GitHub tokens, Runner files or systemd Units are touched.
This fixture checks the production single-threaded event pump rather than
mocking the process and socket communication layers.
"""
import json
import os
import pathlib
import socket
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
from v2_event_loop import pump_transaction
from v2_executor_contract import decode_request


@unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
class RealChannelTests(unittest.TestCase):
    def test_child_cli_like_privileged_request_with_real_socket(self):
        transaction = decode_request(b'{"op":"create","repository":"owner/repo"}')
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        output_read, output_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                parent.close()
                os.close(output_read)
                child.settimeout(3)
                request = {"op":"create_state","repository":"owner/repo",
                           "runner_dir":"/home/actions/actions-runner-owner--repo",
                           "runner_name":"local-ci-owner--repo","stage":"PRE_REGISTRATION"}
                child.sendall(json.dumps(request).encode() + b"\n")
                data = child.recv(4096)
                if json.loads(data)["ok"] is not True:
                    os._exit(2)
                os.write(output_write, b"bounded non-secret diagnostic\n")
                os._exit(0)
            except Exception:
                os._exit(3)
        child.close()
        os.close(output_write)
        try:
            with patch("v2_event_loop.handle_root_request",
                       return_value=b'{"ok":true}\n') as privileged:
                status, ambiguous = pump_transaction(
                    Mock(), transaction, pid, parent, output_read,
                    time.monotonic() + 5,
                )
            self.assertFalse(ambiguous)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0)
            privileged.assert_called_once()
        finally:
            parent.close()

if __name__ == "__main__":
    unittest.main()
