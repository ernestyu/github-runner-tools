#!/usr/bin/env python3
"""Disposable fake-runner integration tests; no GitHub or systemd mutation."""
from __future__ import annotations
import json
import signal
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "SYNTHETIC_REMOVAL_TOKEN_123456"
STUB = """#!/usr/bin/env python3
import json, os, sys, signal
fd = os.environ["GRT_CHECK_RESULT_FD"]
with open(os.environ["GRT_ARG_FILE"], "w") as out:
    json.dump({"argv":sys.argv[1:],"result_fd_inherited":os.path.exists("/proc/self/fd/"+fd)}, out)
print("CHILD_STDOUT_" + os.environ["GRT_TEST_SECRET"])
print("CHILD_STDERR_" + os.environ["GRT_TEST_SECRET"], file=sys.stderr)
if os.environ.get("GRT_CONFIG_SIGNAL") == "TERM":
    os.kill(os.getpid(), signal.SIGTERM)
sys.exit(int(os.environ.get("GRT_CONFIG_EXIT","0")))
"""
DRIVER = r"""
RUNNER_TOOLS_LIB_ONLY=1
source "$1"
web_context_check() { :; }
web_service_state() {
    if [[ "$GRT_FAIL_STAGE" == "service_state" ]]; then return 1; fi
    printf active
}
web_service_operation() {
    printf '%s\n' "$1" >> "$GRT_EVENT_FILE"
    if [[ "$GRT_FAIL_STAGE" == "$1" ]]; then return 1; fi
}
if [[ "$GRT_FAIL_STAGE" == "local_cleanup" ]]; then
    rm() {
        if [[ "$*" == *actions-runner-example--repo* ]]; then return 42; fi
        command rm "$@"
    }
fi
main --base-dir "$GRT_BASE_DIR" --web-worker --token-fd "$2" --result-fd "$3" --privileged-fd 99 --lock-already-held example/repo
"""

class OfficialWebRemoveScriptTests(unittest.TestCase):
    def run_case(self, *, token=TOKEN, config_exit=0, fail_stage="", config_signal=""):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            runner = base / "actions-runner-example--repo"
            runner.mkdir()
            (runner / ".runner").write_text(json.dumps({"agentName": "fixture"}))
            (runner / ".service").write_text("actions.runner.example-repo.fixture.service\n")
            (runner / "svc.sh").write_text("#!/bin/sh\nexit 0\n")
            (runner / "config.sh").write_text(STUB)
            (runner / "svc.sh").chmod(0o755)
            (runner / "config.sh").chmod(0o755)
            events, args_file = base / "events.log", base / "arguments.json"
            token_r, token_w = os.pipe()
            result_r, result_w = os.pipe()
            try:
                os.write(token_w, (token if isinstance(token, bytes) else token.encode()) + b"\n")
                os.close(token_w)
                token_w = -1
                env = {**os.environ,
                    "GRT_WEB_CONTEXT": "1",
                    "GRT_BASE_DIR": str(base),
                    "GRT_ARG_FILE": str(args_file),
                    "GRT_EVENT_FILE": str(events),
                    "GRT_CHECK_RESULT_FD": str(result_w),
                    "GRT_TEST_SECRET": TOKEN,
                    "GRT_CONFIG_EXIT": str(config_exit),
                    "GRT_CONFIG_SIGNAL": config_signal,
                    "GRT_FAIL_STAGE": fail_stage}
                proc = subprocess.run(
                    ["bash", "-c", DRIVER, "_", str(ROOT / "scripts/remove-runner.sh"), str(token_r), str(result_w)],
                    env=env, pass_fds=(token_r, result_w),
                    text=True, capture_output=True, timeout=10, check=False)
            finally:
                os.close(token_r)
                if token_w >= 0: os.close(token_w)
                os.close(result_w)
            marker = os.read(result_r, 129).decode()
            os.close(result_r)
            captured = json.loads(args_file.read_text()) if args_file.exists() else None
            operations = events.read_text().splitlines() if events.exists() else []
            self.assertNotIn(TOKEN, proc.stdout)
            self.assertNotIn(TOKEN, proc.stderr)
            return proc.returncode, marker, captured, operations, runner.exists()

    def test_official_argv_and_result_fd_not_in_config(self):
        rc, marker, captured, ops, exists = self.run_case()
        self.assertEqual((rc, marker, ops, exists), (0, "", ["service_stop", "service_uninstall"], False))
        self.assertEqual(captured["argv"], ["remove", "--token", TOKEN])
        self.assertIs(captured["result_fd_inherited"], False)

    def test_config_failure_preserves_directory_and_provides_code(self):
        rc, marker, captured, ops, exists = self.run_case(config_exit=7)
        self.assertNotEqual(rc, 0)
        self.assertEqual(marker, "GRT_REMOVE_RESULT_V1 stage=config_remove_failed exit=7\n")
        self.assertEqual(captured["argv"], ["remove", "--token", TOKEN])
        self.assertEqual(ops, ["service_stop", "service_uninstall"])
        self.assertTrue(exists)

    def test_preflight_token_failure_has_no_service_effect(self):
        rc, marker, captured, ops, exists = self.run_case(token="")
        self.assertNotEqual(rc, 0)
        self.assertEqual(marker, "GRT_REMOVE_RESULT_V1 stage=preflight_failed exit=unknown\n")
        self.assertIsNone(captured)
        self.assertEqual(ops, [])
        self.assertTrue(exists)

    def test_service_failures_halt_before_registration(self):
        for stage, expected, operations in (
            ("service_state", "service_state_failed", []),
            ("service_stop", "service_stop_failed", ["service_stop"]),
            ("service_uninstall", "service_uninstall_failed", ["service_stop", "service_uninstall"])):
            with self.subTest(stage=stage):
                rc, marker, captured, ops, exists = self.run_case(fail_stage=stage)
                self.assertNotEqual(rc, 0)
                self.assertEqual(marker, f"GRT_REMOVE_RESULT_V1 stage={expected} exit=unknown\n")
                self.assertIsNone(captured)
                self.assertEqual(ops, operations)
                self.assertTrue(exists)

    def test_cleanup_failure_preserves_directory(self):
        rc, marker, captured, ops, exists = self.run_case(fail_stage="local_cleanup")
        self.assertNotEqual(rc, 0)
        self.assertEqual(marker, "GRT_REMOVE_RESULT_V1 stage=local_cleanup_failed exit=unknown\n")
        self.assertEqual(captured["argv"], ["remove", "--token", TOKEN])
        self.assertTrue(exists)


    def test_token_byte_validation_before_service_mutation(self):
        for token in (b"bad\\x00token", b"bad\\rtoken", b"bad\\ntoken", b"", b"X" * 1025,
                      b"\\xffinvalid"):
            with self.subTest(token=repr(token[:30])):
                rc, marker, captured, ops, exists = self.run_case(token=token)
                self.assertNotEqual(rc, 0)
                self.assertEqual(marker, "GRT_REMOVE_RESULT_V1 stage=preflight_failed exit=unknown\\n")
                self.assertIsNone(captured)
                self.assertEqual(ops, [])
                self.assertTrue(exists)

    def test_ambiguous_high_exit_and_signal_are_unknown(self):
        for args in ({"config_exit": 143}, {"config_signal": "TERM"}):
            with self.subTest(args=args):
                rc, marker, captured, ops, exists = self.run_case(**args)
                self.assertNotEqual(rc, 0)
                self.assertEqual(marker, "GRT_REMOVE_RESULT_V1 stage=unknown_failed exit=unknown\\n")
                self.assertTrue(exists)
                self.assertEqual(ops, ["service_stop", "service_uninstall"])

if __name__ == "__main__":
    unittest.main()
