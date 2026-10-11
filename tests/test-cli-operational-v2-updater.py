#!/usr/bin/env python3
"""Frozen logging upgrade contract assertions without touching host systemd."""
from pathlib import Path
import re
import unittest
BASE=Path(__file__).resolve().parents[1]
class UpgradeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text=(BASE/"scripts/update-cli-operational-logging-v2.sh").read_text()
    def test_exact_original_unit(self):
        s=self.text
        self.assertIn("sudo cmp -s \"$BASELINE\" \"$UNIT\"",s)
        self.assertIn("UNKNOWN_EXECUTOR_UNIT_BASELINE",s)
        self.assertIn("ReadWritePaths=/home/actions /etc/systemd/system",s)
        self.assertIn('ProtectSystem=strict',s)
    def test_log_creation_is_rollback_protected(self):
        s=self.text
        self.assertLess(s.index("trap rollback EXIT"),s.index('sudo install -d -o root -g actions -m 2770 "$LOG_ROOT"'))
        self.assertLess(s.index("trap rollback EXIT"),s.index('sudo install -o actions -g actions -m 0600 /dev/null "$LOG_ROOT/operational.lock"'))
        self.assertIn("LOG_DIR_CREATED=1",s)
        self.assertIn("LOG_LOCK_CREATED=1",s)
    def test_verified_recovery_and_no_runner_mutation(self):
        s=self.text
        self.assertIn("ROLLBACK_INCOMPLETE",s)
        self.assertIn("ROLLBACK_VERIFIED",s)
        self.assertIn('sudo cmp -s "$BACKUP/executor.service" "$UNIT"',s)
        self.assertIn('sudo cmp -s "$BACKUP/cli/$name" "$CLI_ROOT/$name"',s)
        self.assertIn("sudo test -S /run/github-runner-tools/executor.sock",s)
        self.assertNotRegex(s,r"systemctl (?:restart|stop|disable|enable) .*actions\\.runner")
    def test_only_executor_unit_restart(self):
        s=self.text
        self.assertIn("sudo systemctl restart github-runner-tools-executor.service",s)
        self.assertNotIn("sudo systemctl restart github-runner-tools-web.service",s)
    def test_source_provenance_and_atomic_unit_install(self):
        s=self.text
        self.assertIn("git -C \"$ROOT\" rev-parse HEAD",s)
        self.assertIn('sudo mv -f -- "$TEMP_UNIT" "$UNIT"',s)
        self.assertIn('sudo mv -f -- "$TEMP_TARGET" "$CLI_ROOT/$name"',s)
if __name__=="__main__":unittest.main()
