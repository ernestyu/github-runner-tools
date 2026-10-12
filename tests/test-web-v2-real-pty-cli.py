#!/usr/bin/env python3
"""Exercise the actual V2 PTY adapter against a fake official config CLI.

The token remains in inherited FD, never argv/env. No live GitHub calls.
"""
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "web" / "pty_token_adapter.py"
CONFIG = ROOT / "tests" / "fixtures" / "v2_fake_config.py"


class PTYLifecycleTests(unittest.TestCase):
    def run_adapter(self, mode):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"fixture-only-token\n")
        finally:
            os.close(write_fd)
        try:
            args = [sys.executable, str(ADAPTER),
                    "--token-fd", str(read_fd), "--mode", mode,
                    "--timeout", "5", "--",
                    sys.executable, str(CONFIG), mode]
            self.assertNotIn("fixture-only-token", " ".join(args))
            result = subprocess.run(args, pass_fds=(read_fd,), timeout=10,
                                    capture_output=True, text=True,
                                    env={"PATH": "/usr/bin:/bin"})
            return result
        finally:
            os.close(read_fd)

    def test_create_token_has_no_argv_or_echo(self):
        result = self.run_adapter("create")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fixture-only-token", result.stdout + result.stderr)

    def test_remove_token_has_no_argv_or_echo(self):
        result = self.run_adapter("remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fixture-only-token", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
