#!/usr/bin/env python3
"""Only fixed, audited failure labels may enter the root Executor journal."""
import contextlib
import io
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
from v2_cli_diagnostics import classify_output, log_cli_exit

class SafeJournalTests(unittest.TestCase):
    def test_fixed_launcher_categories(self):
        text = b"v2_cli_launcher: restricted launch rejected capability_drop_failed\n"
        self.assertEqual(classify_output(text), "launcher_capability_drop_failed")

    def test_known_preflight_category_and_no_raw_secret(self):
        secret = b"ghs_TEST_NEVER_JOURNAL_123456789"
        transcript = b"ERROR: Local archive config is missing: " + secret + b"\n"
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            log_cli_exit("create", 1 << 8, transcript)
        out = stream.getvalue()
        self.assertIn("cause=archive_config_missing", out)
        self.assertIn("exit=1", out)
        self.assertNotIn(secret.decode(), out)
        self.assertNotIn("Local archive config", out)

    def test_unknown_output_cannot_become_journal_content(self):
        secret = b"token-super-secret-in-CLI-output"
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            log_cli_exit("remove", 42 << 8, secret)
        self.assertEqual(stream.getvalue().strip(),
                         "v2_cli_failure op=remove exit=42 cause=unclassified_cli_failure")

    def test_success_is_silent(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            log_cli_exit("create", 0, b"secret")
            log_cli_exit("not-allowed", 1 << 8, b"secret")
        self.assertEqual(stream.getvalue(), "")

if __name__ == "__main__":
    unittest.main()
