#!/usr/bin/env python3
"""Shared CLI Remove root checkpoint has a fixed identity and existing journal."""
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import cli_remove_authority as m

ARGS = ["owner/repo", "runner", "/home/actions/actions-runner-owner--repo", "COMPLETE"]
ACCOUNT = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_dir="/home/actions")

class RemoveCLIJournalTests(unittest.TestCase):
    def test_complete_delegates_to_exact_existing_authority(self):
        with patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m.pwd, "getpwnam", return_value=ACCOUNT), \
             patch.object(m.os.path, "lexists", return_value=False), \
             patch.object(m, "remove_stage") as authority:
            m.checkpoint(ARGS, sudo_user="actions")
        authority.assert_called_once_with(
            "owner/repo", "runner",
            "/home/actions/actions-runner-owner--repo", "actions", "COMPLETE")

    def test_nonroot_never_reaches_authority(self):
        with patch.object(m.os, "geteuid", return_value=1000), \
             patch.object(m, "remove_stage") as authority:
            with self.assertRaises(m.RemoveCLIError):
                m.checkpoint(ARGS, sudo_user="actions")
            authority.assert_not_called()

    def test_target_mismatch_never_reaches_authority(self):
        with patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m.pwd, "getpwnam", return_value=ACCOUNT), \
             patch.object(m, "remove_stage") as authority:
            with self.assertRaises(m.RemoveCLIError):
                m.checkpoint(["owner/repo","runner","/tmp/foreign-runner","BEGIN"],
                             sudo_user="actions")
            authority.assert_not_called()

    def test_absent_directory_required_for_complete(self):
        with patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m.pwd, "getpwnam", return_value=ACCOUNT), \
             patch.object(m.os.path, "lexists", return_value=True), \
             patch.object(m, "remove_stage") as authority:
            with self.assertRaises(m.RemoveCLIError):
                m.checkpoint(ARGS, sudo_user="actions")
            authority.assert_not_called()

    def test_unauthorized_stage_rejected(self):
        with patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m.pwd, "getpwnam", return_value=ACCOUNT), \
             patch.object(m, "remove_stage") as authority:
            with self.assertRaises(m.RemoveCLIError):
                m.checkpoint([*ARGS[:-1],"RESET_UNKNOWN"], sudo_user="actions")
            authority.assert_not_called()

if __name__ == "__main__":
    unittest.main()
