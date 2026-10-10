#!/usr/bin/env python3
"""V2 token transport: real inherited FD, no token in argv/env/files."""
import os
import hashlib
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
from v2_token_fd import make_token_pipe, consume_token_fd, close_quietly, TokenTransportError
from v2_executor_contract import AdmissionError


class TransportTests(unittest.TestCase):
    def test_one_shot_pipe(self):
        secret = b"sample-temporary-secret"
        fd = make_token_pipe(secret)
        try:
            self.assertEqual(consume_token_fd(fd), secret)
            with self.assertRaises(TokenTransportError):
                consume_token_fd(fd)
        finally:
            close_quietly(fd)

    def test_cross_process_fd(self):
        secret = b"isolated-temporary-token"
        fd = make_token_pipe(secret)
        program = (
            "import os,sys,hashlib;"
            "fd=int(sys.argv[1]);"
            "content=os.read(fd,2048);"
            "os.close(fd);"
            "sys.stdout.write(hashlib.sha256(content).hexdigest())"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", program, str(fd)],
                pass_fds=(fd,), capture_output=True, timeout=3,
                env={"PATH": "/usr/bin:/bin"},
                check=True,
            )
            self.assertEqual(result.stdout.decode(), hashlib.sha256(secret + b"\\n").hexdigest())
            self.assertNotIn(secret, " ".join(result.args).encode())
        finally:
            close_quietly(fd)

    def test_no_token_echo_on_invalid_input(self):
        for secret in (b"", b"secret\nspoof", b"secret\x00", b"z" * 1025):
            with self.subTest(sample=secret[:3]), self.assertRaises(AdmissionError) as caught:
                make_token_pipe(secret)
            self.assertNotIn("secret", str(caught.exception))

    def test_rejects_bogus_fd(self):
        with self.assertRaises(TokenTransportError):
            consume_token_fd(-1)

    def test_rejects_malformed_pipe(self):
        r, w = os.pipe()
        try:
            os.write(w, b"secret\r\n")
            os.close(w)
            with self.assertRaises(TokenTransportError):
                consume_token_fd(r)
        finally:
            close_quietly(r)


if __name__ == "__main__":
    unittest.main()
