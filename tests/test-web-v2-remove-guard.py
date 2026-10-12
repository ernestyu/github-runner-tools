#!/usr/bin/env python3
"""Remove admission must bind exact ID + cycle and never adopt legacy runner."""
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
import v2_remove_guard as guard
from v2_executor_contract import decode_request

REMOVE = b'{"op":"remove","repository":"owner/repo","runner_name":"test","runner_id":37,"instance":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.runtime = MagicMock()
        self.runtime.runner_home = "/home/actions"
        self.runtime.validate_runner_dir.return_value = "/home/actions/actions-runner-owner--repo"
        self.req = decode_request(REMOVE)

    def test_missing_active_instance_denied(self):
        with patch.object(guard, "_current", return_value=None), \
             patch.object(guard, "_cycle_record") as cycle:
            with self.assertRaises(guard.RemoveGuardError):
                guard.check_remove_target(self.runtime, self.req)
        cycle.assert_not_called()

    def test_changed_instance_denied(self):
        with patch.object(guard, "_current", return_value={"instance":"bb"*16}), \
             patch.object(guard, "_cycle_record") as cycle:
            with self.assertRaises(guard.RemoveGuardError):
                guard.check_remove_target(self.runtime, self.req)
        cycle.assert_not_called()

    def test_unknown_create_state_denied(self):
        active = {"instance":"aa"*16}
        with patch.object(guard, "_current", return_value=active), \
             patch.object(guard, "_cycle_record", return_value={"stage":"REGISTRATION_OUTCOME_UNKNOWN"}):
            with self.assertRaises(guard.RemoveGuardError):
                guard.check_remove_target(self.runtime, self.req)

    def test_existing_remove_ledger_blocks_retry(self):
        active = {"instance":"aa"*16}
        def cycle(kind, *_args):
            if kind == guard.CREATE_STATES:
                return {"stage":"CREATE_COMPLETE"}
            if kind == guard.REMOVE_STATES:
                return {"stage":"REMOTE_REMOVE_OUTCOME_UNKNOWN"}
            return None
        with patch.object(guard, "_current", return_value=active), \
             patch.object(guard, "_cycle_record", side_effect=cycle):
            with self.assertRaises(guard.RemoveGuardError):
                guard.check_remove_target(self.runtime, self.req)

    def test_missing_registration_provenance_denied(self):
        active = {"instance":"aa"*16}
        def cycle(kind, *_args):
            if kind == guard.CREATE_STATES:
                return {"stage":"CREATE_COMPLETE"}
            return None
        with patch.object(guard, "_current", return_value=active), \
             patch.object(guard, "_cycle_record", side_effect=cycle):
            with self.assertRaises(guard.RemoveGuardError):
                guard.check_remove_target(self.runtime, self.req)

if __name__ == "__main__":
    unittest.main()
