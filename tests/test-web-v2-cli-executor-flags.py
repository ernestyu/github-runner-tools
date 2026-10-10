#!/usr/bin/env python3
"""Fail-closed regression for the new private Executor CLI entrypoint.

These tests intentionally do not create or remove runners.
"""
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class PrivateExecutorFlagTests(unittest.TestCase):
    def invoke(self, script, *args, env=None):
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / script), *args],
            capture_output=True, text=True, timeout=6, env=env,
        )

    def test_normal_cli_internal_options_rejected(self):
        for script in ("register-runner.sh", "remove-runner.sh"):
            with self.subTest(script=script):
                result = self.invoke(script, "--token-fd", "3", "owner/repo")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not available in normal CLI mode", result.stderr)

    def test_executor_mode_rejects_without_attested_context(self):
        for script in ("register-runner.sh", "remove-runner.sh"):
            with self.subTest(script=script):
                result = self.invoke(
                    script, "--executor-mode", "--token-fd", "3",
                    "--privileged-fd", "4", "--lock-already-held", "owner/repo",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("trusted context", result.stderr)

    def test_executor_remove_keeps_secret_out_of_config_argv(self):
        script = (ROOT / "scripts" / "remove-runner.sh").read_text()
        self.assertIn('if [[ "$EXECUTOR_MODE" != "1" ]]; then\n    web_token_from_fd', script)
        self.assertIn('--token-fd "$TOKEN_FD" --mode remove -- ./config.sh remove', script)
        self.assertIn('if [[ "$EXECUTOR_MODE" == "1" ]]; then\n    # V2 consumes', script)
        # V1 backward compatibility may still use the legacy --token form;
        # the V2 branch must precede it and never reference the Bash secret.
        start = script.index('if [[ "$EXECUTOR_MODE" == "1" ]]; then\n    # V2 consumes')
        end = script.index('elif ( exec {RESULT_FD}>&-;', start)
        self.assertNotIn('"$TOKEN"', script[start:end])
    def test_executor_mode_rejects_fake_fd(self):
        for script in ("register-runner.sh", "remove-runner.sh"):
            with self.subTest(script=script):
                result = self.invoke(
                    script, "--executor-mode", "--token-fd", "3",
                    "--privileged-fd", "4", "--lock-already-held", "owner/repo",
                    env={"PATH": "/usr/bin:/bin", "GRT_EXECUTOR_CONTEXT": "1"},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Runner release", result.stdout)
                self.assertNotIn("Removing runner registration", result.stdout)

if __name__ == "__main__":
    unittest.main()
