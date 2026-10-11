#!/usr/bin/env python3
"""Pure tests of root-to-CLI posix_spawn descriptor actions."""
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_cli_spawn as launch
from v2_executor_contract import decode_request


class SpawnTests(unittest.TestCase):
    def test_fixed_fd_mapping(self):
        actions = launch.child_file_actions(13, 14, 15)
        self.assertEqual(actions[:4], [
            (os.POSIX_SPAWN_DUP2, 13, 3),
            (os.POSIX_SPAWN_DUP2, 14, 4),
            (os.POSIX_SPAWN_DUP2, 15, 1),
            (os.POSIX_SPAWN_DUP2, 15, 2),
        ])
        self.assertEqual(actions[4:], [
            (os.POSIX_SPAWN_CLOSE, 13),
            (os.POSIX_SPAWN_CLOSE, 14),
            (os.POSIX_SPAWN_CLOSE, 15),
        ])

    def test_refuse_fd_collisions_and_root_stdio(self):
        for args in ((3, 14, 15), (13, 4, 15), (13, 14, 2),
                     (13, 13, 15), (13, 14, 14)):
            with self.subTest(args=args), self.assertRaises(launch.SpawnError):
                launch.child_file_actions(*args)

    def test_no_lifecycle_spawn_as_unprivileged_user(self):
        request = decode_request(b'{"op":"create","repository":"owner/repo"}')
        with patch.object(launch.os, "geteuid", return_value=1000), \
             patch.object(launch.os, "posix_spawn") as spawn:
            with self.assertRaisesRegex(launch.SpawnError, "root Executor"):
                launch.spawn_cli(request, b"dummy-token", 14)
            spawn.assert_not_called()

    def test_no_status_lifecycle_spawn(self):
        request = decode_request(b'{"op":"status"}')
        with patch.object(launch.os, "posix_spawn") as spawn:
            with self.assertRaises(launch.SpawnError):
                launch.spawn_cli(request, b"dummy-token", 14)
            spawn.assert_not_called()

    def test_missing_installed_cli_blocks_root_spawn(self):
        request = decode_request(b'{"op":"create","repository":"owner/repo"}')
        from v2_code_provenance import CodeProvenanceError
        with patch.object(launch.os, "geteuid", return_value=0), \
             patch("v2_code_provenance.verify_launch",
                   side_effect=CodeProvenanceError("untrusted")), \
             patch.object(launch.os, "posix_spawn") as spawn:
            with self.assertRaisesRegex(launch.SpawnError, "provenance"):
                launch.spawn_cli(request, b"dummy-token", 14)
            spawn.assert_not_called()

    def test_reject_privileged_socket_under_fd5(self):
        request = decode_request(b'{"op":"remove","repository":"o/r","runner_name":"runner","runner_id":123,"instance":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
        with patch.object(launch.os, "geteuid", return_value=0), \
             patch.object(launch.os, "posix_spawn") as spawn:
            with self.assertRaises(launch.SpawnError):
                launch.spawn_cli(request, b"dummy-token", 4)
            spawn.assert_not_called()

if __name__ == "__main__":
    unittest.main()
