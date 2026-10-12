#!/usr/bin/env python3
"""V2 authenticated transport tests; no root or runner mutation."""
import pathlib
import socket
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
from v2_executor_contract import decode_request
import v2_executor_transport as transport

class FrameTests(unittest.TestCase):
    def roundtrip(self, raw, secret):
        request = decode_request(raw)
        a, b = socket.socketpair()
        try:
            a.sendall(transport.encode_request_frame(request, secret))
            return transport.read_request_frame(b)
        finally:
            a.close()
            b.close()

    def test_create_real_socket(self):
        request, secret = self.roundtrip(b'{"op":"create","repository":"owner/repo"}', b"temporary-token")
        self.assertEqual(request.repository, "owner/repo")
        self.assertEqual(secret, b"temporary-token")

    def test_status_real_socket(self):
        request, secret = self.roundtrip(b'{"op":"status"}', None)
        self.assertEqual(request.op, "status")
        self.assertIsNone(secret)

    def test_remove_has_no_nonce(self):
        raw = b'{"op":"remove","repository":"owner/repo","runner_name":"test","runner_id":24,"instance":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
        request, secret = self.roundtrip(raw, b"short-token")
        self.assertEqual(request.runner_id, 24)
        self.assertEqual(secret, b"short-token")

    def test_no_token_in_json_frame(self):
        request = decode_request(b'{"op":"create","repository":"owner/repo"}')
        frame = transport.encode_request_frame(request, b"temporary-token")
        size = int.from_bytes(frame[:4], "big")
        self.assertNotIn(b"temporary-token", frame[4:4+size])

    def test_status_secret_rejected(self):
        request = decode_request(b'{"op":"status"}')
        with self.assertRaises(transport.ExecutorError):
            transport.encode_request_frame(request, b"invalid")

    def test_invalid_peer_uid(self):
        left, right = socket.socketpair()
        try:
            with patch.object(transport.os, "geteuid", return_value=0), \
                 patch.object(transport.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=987654)):
                with self.assertRaisesRegex(transport.ExecutorError, "unauthorized"):
                    transport.admit_peer(left)
        finally:
            left.close()
            right.close()

    def test_incomplete_frame(self):
        left, right = socket.socketpair()
        try:
            left.sendall(b"\x00\x00\x00\x05hi")
            left.shutdown(socket.SHUT_WR)
            with self.assertRaises(transport.ExecutorError):
                transport.read_request_frame(right)
        finally:
            left.close()
            right.close()

if __name__ == "__main__":
    unittest.main()
