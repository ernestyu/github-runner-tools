#!/usr/bin/env python3
"""No-root real posix_spawn test of V2 token FD3 and privilege socket FD4.

Runs only an isolated fixture, no CLI installation, GitHub API or systemd.
"""
import hashlib
import os
import pathlib
import selectors
import socket
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
from v2_cli_spawn import child_file_actions
from v2_token_fd import make_token_pipe, close_quietly


class RealSpawnTests(unittest.TestCase):
    def test_separate_child_reads_token_and_performs_private_roundtrip(self):
        read_token = make_token_pipe(b"fixture-only-token")
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        out_read, out_write = os.pipe2(os.O_CLOEXEC)
        import fcntl
        def above_reserved(fd):
            if fd >= 5:
                return fd
            new = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 5)
            os.close(fd)
            return new
        read_token = above_reserved(read_token)
        child_source = fcntl.fcntl(child.fileno(), fcntl.F_DUPFD_CLOEXEC, 5)
        out_write = above_reserved(out_write)
        try:
            self.assertTrue(len({read_token, child_source, out_write}) == 3)
            actions = child_file_actions(read_token, child_source, out_write)
            argv = [sys.executable, str(ROOT / "tests/fixtures/v2_fd_child.py")]
            self.assertNotIn("fixture-only-token", " ".join(argv))
            pid = os.posix_spawn(sys.executable, argv,
                                 {"PATH": "/usr/bin:/bin"},
                                 file_actions=actions)
        finally:
            close_quietly(read_token)
            close_quietly(child_source)
            close_quietly(out_write)
            child.close()
        try:
            parent.settimeout(3)
            incoming = bytearray()
            while not incoming.endswith(b"\n"):
                fragment = parent.recv(4096)
                self.assertTrue(fragment)
                incoming.extend(fragment)
            self.assertIn(b'"op": "create_state"', incoming)
            self.assertNotIn(b"fixture-only-token", incoming)
            parent.sendall(b'{"ok":true}\n')
            output = os.read(out_read, 1024).decode().strip()
            _, status = os.waitpid(pid, 0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0)
            self.assertEqual(output,
                             hashlib.sha256(b"fixture-only-token\n").hexdigest())
        finally:
            parent.close()
            close_quietly(out_read)


if __name__ == "__main__":
    unittest.main()
