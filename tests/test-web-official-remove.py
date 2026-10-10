#!/usr/bin/env python3
"""Disposable fake-runner integration tests; no GitHub or systemd mutation."""
from __future__ import annotations
import json
import fcntl
import time
import sys
from unittest import mock
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
    python3 -c 'import json,os; f=os.environ["GRT_PROBE_FILE"]; r=os.environ["GRT_CHECK_RESULT_FD"]; t=os.environ["GRT_PROBE_TOKEN_FD"]; record=json.dumps({"result_fd":os.path.exists("/proc/self/fd/"+r),"token_fd":os.path.exists("/proc/self/fd/"+t)}); open(f,"w").write(record)'
    if [[ "$GRT_FAIL_STAGE" == "service_state" ]]; then return 1; fi
    if [[ -e "$GRT_UNINSTALLED_FLAG" || "$GRT_INITIAL_ABSENT" == "1" ]]; then printf absent; else printf active; fi
}
web_service_operation() {
    printf '%s\n' "$1" >> "$GRT_EVENT_FILE"
    if [[ "$GRT_FAIL_STAGE" == "$1" ]]; then return 1; fi
    if [[ "$1" == "service_uninstall" ]]; then : > "$GRT_UNINSTALLED_FLAG"; fi
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
            (runner / ".runner").write_text(json.dumps({"agentName": "fixture", "gitHubUrl": "https://github.com/example/repo"}))
            (runner / ".service").write_text("actions.runner.example-repo.fixture.service\n")
            (runner / "svc.sh").write_text("#!/bin/sh\nexit 0\n")
            (runner / "config.sh").write_text(STUB)
            (runner / "svc.sh").chmod(0o755)
            (runner / "config.sh").chmod(0o755)
            events, args_file = base / "events.log", base / "arguments.json"
            probe_file = base / "fd-probe.json"
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
                    "GRT_PROBE_FILE": str(probe_file),
                    "GRT_PROBE_TOKEN_FD": str(token_r),
                    "GRT_CONFIG_EXIT": str(config_exit),
                    "GRT_CONFIG_SIGNAL": config_signal,
                    "GRT_FAIL_STAGE": fail_stage,
                    "GRT_UNINSTALLED_FLAG": str(base / "uninstalled.flag"),
                    "GRT_INITIAL_ABSENT": "0"}
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
            if probe_file.exists():
                self.assertEqual(json.loads(probe_file.read_text()),
                                 {"result_fd": False, "token_fd": False})
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
        for token in (b"bad\x00token", b"bad\rtoken", b"bad\ntoken", b"", b"X" * 1025,
                      b"\xffinvalid"):
            with self.subTest(token=repr(token[:30])):
                rc, marker, captured, ops, exists = self.run_case(token=token)
                self.assertNotEqual(rc, 0)
                self.assertEqual(marker, "GRT_REMOVE_RESULT_V1 stage=preflight_failed exit=unknown\n")
                self.assertIsNone(captured)
                self.assertEqual(ops, [])
                self.assertTrue(exists)

    def test_ambiguous_high_exit_and_signal_are_unknown(self):
        for args in ({"config_exit": 143}, {"config_signal": "TERM"}):
            with self.subTest(args=args):
                rc, marker, captured, ops, exists = self.run_case(**args)
                self.assertNotEqual(rc, 0)
                self.assertEqual(marker, "GRT_REMOVE_RESULT_V1 stage=unknown_failed exit=unknown\n")
                self.assertTrue(exists)
                self.assertEqual(ops, ["service_stop", "service_uninstall"])


class FdAndTimeoutEvidenceTests(unittest.TestCase):
    def test_real_wrappers_pipeline_substitution_and_failure_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir = root / "bin"
            bindir.mkdir()
            log = root / "probe.jsonl"
            code = ("#!/usr/bin/python3\n"
                "import os,sys,json\n"
                "rec={'tool':os.path.basename(sys.argv[0]),'fd':os.path.exists('/proc/self/fd/'+os.environ['PROBE_FD'])}\n"
                "with open(os.environ['PROBE_LOG'],'a') as f:f.write(json.dumps(rec)+'\\n')\n"
                "if '--fail' in sys.argv:sys.exit(17)\n"
                "print('ok')\n")
            for name in ("python3","jq","cat","tr","sed"):
                p = bindir / name
                p.write_text(code)
                p.chmod(0o755)
            rd, wr = os.pipe()
            driver = r"""
RUNNER_TOOLS_LIB_ONLY=1
source "$REMOVE_SCRIPT"
WEB_MODE=1
RESULT_FD="$1"
REMOVE_STAGE=service_stop_failed
python3 foo
jq foo
cat foo
tr foo
sed foo
value="$(jq substitute)"
printf 'a\n' | tr pipe >/dev/null
if sed --fail; then exit 11; fi
exit 1
"""
            try:
                env = {**os.environ,"PATH":str(bindir)+":"+os.environ["PATH"],
                       "PROBE_FD":str(wr),"PROBE_LOG":str(log),
                       "REMOVE_SCRIPT":str(ROOT/"scripts/remove-runner.sh")}
                proc = subprocess.run(["bash","-c",driver,"_",str(wr)],
                    pass_fds=(wr,),env=env,capture_output=True,text=True,timeout=10)
            finally:
                os.close(wr)
            try:
                marker = os.read(rd,129)
                extra = os.read(rd,129)
            finally:
                os.close(rd)
            self.assertEqual(proc.returncode,1)
            self.assertEqual(marker,b"GRT_REMOVE_RESULT_V1 stage=service_stop_failed exit=unknown\n")
            self.assertEqual(extra,b"")
            observed = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertTrue(all(not x["fd"] for x in observed))
            names = [x["tool"] for x in observed]
            for name in ("python3","jq","cat","tr","sed"):
                self.assertIn(name,names)
            self.assertGreaterEqual(names.count("jq"),2)
            self.assertGreaterEqual(names.count("tr"),2)

    def test_dispatcher_timeout_process_group_single_execution_and_lock(self):
        sys.path.insert(0, str(ROOT / "web"))
        import dispatcher
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sentinel = root / "actions-runner-example--repo"
            sentinel.mkdir()
            (sentinel / ".runner").write_text("SENTINEL: untouched")
            lock = root / "lock"
            lock.touch()
            calls = root / "calls"
            parent_pidfile = root / "worker.pid"
            child_pidfile = root / "child.pid"
            worker = root / "blocked_worker.py"
            worker.write_text(
                "import os,subprocess,sys,time\n"
                "with open(" + repr(str(calls)) + ",'a') as f:f.write('called\\n')\n"
                "with open(" + repr(str(parent_pidfile)) + ",'w') as f:f.write(str(os.getpid()))\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                "with open(" + repr(str(child_pidfile)) + ",'w') as f:f.write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            rt = SimpleNamespace(
                lock_file=str(lock), timeout=1.0,
                runner_user="nobody", runner_home=str(root), cli_dir=str(root),
                pty_adapter=str(root / "unused"), worker_path=str(worker))
            rt.validate_fixed_worker = lambda: None
            server = object.__new__(dispatcher.DispatchServer)
            server.runtime = rt
            original_popen = dispatcher.subprocess.Popen
            spawned = []
            def captured_popen(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                spawned.append(process)
                return process
            with mock.patch.object(dispatcher.subprocess, "Popen", side_effect=captured_popen):
                result = dispatcher.DispatchServer.execute(server, {
                    "op": "remove", "repository": "example/repo", "token": "FAKE_TOKEN"})
            self.assertEqual(result.get("error"), "operation_timed_out")
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].stdout)
            self.assertIsNotNone(spawned[0].stderr)
            self.assertTrue(spawned[0].stdout.closed, "stdout pipe leaked on timeout")
            self.assertTrue(spawned[0].stderr.closed, "stderr pipe leaked on timeout")
            self.assertEqual(calls.read_text().splitlines(), ["called"])
            self.assertTrue(child_pidfile.exists(), "fake descendant was not started")
            parent_pid = int(parent_pidfile.read_text())
            child_pid = int(child_pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(parent_pid, 0)

            # A killed orphan may briefly remain as an OS zombie. A zombie
            # cannot execute; accept either absent or /proc state Z.
            def active(pid):
                try:
                    state = (Path("/proc") / str(pid) / "stat").read_text().split(") ", 1)[1][0]
                except FileNotFoundError:
                    return False
                return state != "Z"
            for _ in range(40):
                if not active(child_pid):
                    break
                time.sleep(0.05)
            self.assertFalse(active(child_pid), "Dispatcher left descendant running")
            self.assertEqual((sentinel / ".runner").read_text(), "SENTINEL: untouched")
            with lock.open("r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

if __name__ == "__main__":
    unittest.main()
