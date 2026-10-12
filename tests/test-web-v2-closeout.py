#!/usr/bin/env python3
"""Create/Remove success is impossible without the existing durable closeout."""
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_closeout as c
from v2_executor_contract import decode_request

CREATE = decode_request(b'{"op":"create","repository":"owner/repo"}')
REMOVE = decode_request(b'{"op":"remove","repository":"owner/repo","runner_name":"runner","runner_id":7,"instance":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
RUNTIME = SimpleNamespace(runner_home="/home/actions")

class CloseoutTests(unittest.TestCase):
    def test_create_requires_durable_complete_record_and_directory(self):
        with patch.object(c, "_current", return_value={"instance":"aa"*16}), \
             patch.object(c, "_cycle_record", return_value={"instance":"aa"*16,"stage":"CREATE_COMPLETE"}), \
             patch.object(c.os.path, "isdir", return_value=True):
            self.assertTrue(c.completed_mutation(RUNTIME, CREATE))
        with patch.object(c, "_current", return_value={"instance":"aa"*16}), \
             patch.object(c, "_cycle_record", return_value={"instance":"aa"*16,"stage":"REGISTERED_HEALTH_UNKNOWN"}), \
             patch.object(c.os.path, "isdir", return_value=True):
            self.assertFalse(c.completed_mutation(RUNTIME, CREATE))

    def test_remove_requires_same_instance_completed_and_target_absent(self):
        with patch.object(c, "_cycle_record", return_value={"instance":"aa"*16,"stage":"REMOVE_COMPLETE"}), \
             patch.object(c.os.path, "lexists", return_value=False):
            self.assertTrue(c.completed_mutation(RUNTIME, REMOVE, remove_instance="aa"*16))
            self.assertFalse(c.completed_mutation(RUNTIME, REMOVE, remove_instance="bb"*16))
        with patch.object(c, "_cycle_record", return_value={"instance":"aa"*16,"stage":"REMOTE_REMOVE_OUTCOME_UNKNOWN"}), \
             patch.object(c.os.path, "lexists", return_value=False):
            self.assertFalse(c.completed_mutation(RUNTIME, REMOVE, remove_instance="aa"*16))
        with patch.object(c, "_cycle_record", return_value={"instance":"aa"*16,"stage":"REMOVE_COMPLETE"}), \
             patch.object(c.os.path, "lexists", return_value=True):
            self.assertFalse(c.completed_mutation(RUNTIME, REMOVE, remove_instance="aa"*16))

    def test_never_creates_state_or_returns_true_on_missing_journal(self):
        with patch.object(c, "_current", return_value=None), \
             patch.object(c, "_cycle_record", return_value=None):
            self.assertFalse(c.completed_mutation(RUNTIME, CREATE))
            self.assertFalse(c.completed_mutation(RUNTIME, REMOVE, remove_instance="aa"*16))

if __name__ == "__main__":
    unittest.main()
