#!/usr/bin/env python3
"""Real CLI native-log subprocess coverage; not mocks of grt_log()."""
import json, os, pathlib, subprocess, tempfile, unittest
BASE=pathlib.Path(__file__).resolve().parents[1]
class RealCliTests(unittest.TestCase):
    def setUp(self):
        self.ctx=tempfile.TemporaryDirectory()
        self.addCleanup(self.ctx.cleanup)
        self.root=pathlib.Path(self.ctx.name)
        self.env={**os.environ, "GITHUB_ACTIONS":"true", "RUNNER_TEMP":str(self.root)}
    def run_cli(self,op,*args):
        name={"create":"register-runner.sh","remove":"remove-runner.sh","status":"status-runners.sh"}[op]
        return subprocess.run(["bash",str(BASE/"scripts"/name),*args],env=self.env,capture_output=True,text=True,timeout=20)
    def records(self):
        path=self.root/f"grt-native-logging-{os.getuid()}"/"operational.jsonl"
        self.assertTrue(path.is_file(),"CLI did not write an operational JSONL file")
        return [json.loads(line) for line in path.read_text().splitlines()]
    def test_create_bad_inputs_are_sanitized(self):
        for bad in ("","invalid","bad/repo/extra","a/"+"x"*230,"evil/repo\nTOKEN_MUST_NOT_APPEAR"):
            with self.subTest(bad=bad[:20]):
                p=self.run_cli("create",bad)
                self.assertNotEqual(p.returncode,0)
                r=self.records()
                self.assertTrue(any(e["op"]=="create" and e["reason"]=="invalid_input" and e["repository"] is None for e in r))
                self.assertNotIn(bad, (self.root/f"grt-native-logging-{os.getuid()}"/"operational.jsonl").read_text())
    def test_remove_bad_inputs_are_sanitized(self):
        for bad in ("","invalid","bad/repo/extra","a/"+"x"*230,"evil/repo\nTOKEN_MUST_NOT_APPEAR"):
            with self.subTest(bad=bad[:20]):
                p=self.run_cli("remove",bad)
                self.assertNotEqual(p.returncode,0)
                r=self.records()
                self.assertTrue(any(e["op"]=="remove" and e["reason"]=="invalid_input" and e["repository"] is None for e in r))
                self.assertNotIn(bad,(self.root/f"grt-native-logging-{os.getuid()}"/"operational.jsonl").read_text())
    def test_status_json_is_unpolluted(self):
        # Empty temp runner inventory exercises full real Status CLI and JSON.
        self.env["RUNNER_BASE_DIR"]=str(self.root)
        p=self.run_cli("status","--json")
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout),[])
        self.assertTrue(any(e["op"]=="status" and e["event"]=="operation_exit" and e["result"]=="ok" for e in self.records()))
    def test_real_logger_file_failure_never_changes_status_stdout(self):
        self.env["RUNNER_BASE_DIR"]=str(self.root)
        logdir=self.root/f"grt-native-logging-{os.getuid()}"
        logdir.mkdir()
        (logdir/"unexpected").write_text("fixed fixture")
        p=self.run_cli("status","--json")
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout),[])
        self.assertEqual((logdir/"unexpected").read_text(),"fixed fixture")
if __name__=="__main__":
    unittest.main()
