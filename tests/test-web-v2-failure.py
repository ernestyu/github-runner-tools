#!/usr/bin/env python3
"""Bounded redacted lifecycle error extraction tests."""
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
from v2_executor_contract import decode_request
import v2_failure as f

REQ = decode_request(b'{"op":"create","repository":"owner/repo"}')

class FailureTests(unittest.TestCase):
    def test_stage_is_existing_durable_record(self):
        runtime = Mock(runner_home="/home/actions")
        with patch.object(f, "_current", return_value={"instance":"aa"*16}), \
             patch.object(f, "_cycle_record", return_value={"stage":"REGISTERED_UNIT_INCOMPLETE"}):
            result = f.diagnostic(runtime, REQ, "lifecycle_failed", exit_code=3)
        self.assertEqual(result["stage"], "REGISTERED_UNIT_INCOMPLETE")
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(len(result["correlation_id"]), 12)

    def test_untrusted_stage_not_returned(self):
        runtime = Mock(runner_home="/home/actions")
        with patch.object(f, "_current", return_value={"instance":"aa"*16}), \
             patch.object(f, "_cycle_record", return_value={"stage":"secret-from-output"}):
            result = f.diagnostic(runtime, REQ, "lifecycle_failed")
        self.assertNotIn("stage", result)

    def test_unknown_remote_outcome_has_explicit_no_retry_category(self):
        runtime = Mock(runner_home="/home/actions")
        with patch.object(f, "_current", return_value={"instance":"aa"*16}), \
             patch.object(f, "_cycle_record",
                          return_value={"stage":"REGISTRATION_OUTCOME_UNKNOWN"}):
            result = f.diagnostic(runtime, REQ, "lifecycle_failed", exit_code=73)
        self.assertEqual(result["outcome"], "UNKNOWN_RECONCILIATION_REQUIRED")
        self.assertFalse(result["ok"])

    def test_nonzero_after_any_checkpoint_is_never_reported_as_safe_retry(self):
        runtime = Mock(runner_home="/home/actions")
        for stage in ("PRE_REGISTRATION", "CREATE_COMPLETE",
                      "REGISTERED_UNIT_INCOMPLETE"):
            with self.subTest(stage=stage), \
                 patch.object(f, "_current", return_value={"instance":"aa"*16}), \
                 patch.object(f, "_cycle_record", return_value={"stage":stage}):
                result = f.diagnostic(runtime, REQ, "lifecycle_failed", exit_code=7)
            self.assertEqual(result["outcome"], "UNKNOWN_RECONCILIATION_REQUIRED")

    def test_definite_pre_external_failure_is_distinct(self):
        runtime = Mock(runner_home="/home/actions")
        with patch.object(f, "_current", return_value=None):
            result = f.diagnostic(runtime, REQ, "lifecycle_failed", exit_code=2)
        self.assertEqual(result["outcome"], "FAILED_PRE_EXTERNAL")

    def test_absent_record_no_synthetic_success(self):
        runtime = Mock(runner_home="/home/actions")
        with patch.object(f, "_current", return_value=None):
            result = f.diagnostic(runtime, REQ, "outcome_unknown")
        self.assertFalse(result["ok"])
        self.assertNotIn("stage", result)

if __name__ == "__main__":
    unittest.main()
