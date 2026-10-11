#!/usr/bin/env python3
"""Deterministic file-rotation and enum-safety tests; no GitHub or runner writes."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

source = Path(__file__).resolve().parents[1] / "scripts" / "cli-operational-logger.py"
spec = importlib.util.spec_from_file_location("grt_operational_logger", source)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class LoggerTests(unittest.TestCase):
    def args(self, **kw):
        fields = dict(op="create", event="preflight", stage="preflight",
                      result="begin", reason="none", exit_code=-1,
                      run_id="f"*32, seq=1, repository="ernestyu/hugo",
                      runner="local-ci-ernestyu--hugo")
        fields.update(kw)
        return SimpleNamespace(**fields)

    def test_no_free_text(self):
        for kw in ({"event": "hello token=abc"}, {"reason": "any arbitrary exception"},
                   {"repository": "foo\nsecret"}, {"runner": "token=123"}):
            with self.assertRaises(mod.LoggingUnavailable):
                mod.event(self.args(**kw))

    def test_jsonl_and_rotation(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / "operational.lock").touch(mode=0o600)
            os.chmod(p / "operational.lock", 0o600)
            previous = mod.MAX_FILE
            mod.MAX_FILE = 350
            try:
                with mod.Logger(root) as log:
                    for i in range(7):
                        record = mod.event(self.args(seq=i))
                        log.append(record)
                names = list(p.glob("operational*.jsonl"))
                self.assertGreater(len(names), 1)
                for name in names:
                    self.assertLessEqual(name.stat().st_size, 350)
                    for line in name.read_text().splitlines():
                        self.assertEqual(json.loads(line)["op"], "create")
            finally:
                mod.MAX_FILE = previous

    def test_14_day_retention(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / "operational.lock").touch(mode=0o600)
            os.chmod(p / "operational.lock", 0o600)
            old = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=15)).strftime("%Y%m%dT%H%M%S%fZ")
            filename = p / f"operational.{old}.{'a'*32}.jsonl"
            filename.write_text('{"v":1}\n')
            os.chmod(filename, 0o600)
            with mod.Logger(root):
                pass
            self.assertFalse(filename.exists())

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / "operational.lock").touch(mode=0o600)
            os.chmod(p / "operational.lock", 0o600)
            outside = p / "outside"
            outside.write_text("leave me alone")
            (p / "operational.jsonl").symlink_to(outside)
            with self.assertRaises((mod.LoggingUnavailable, OSError)):
                with mod.Logger(root):
                    pass
            self.assertEqual(outside.read_text(), "leave me alone")

if __name__ == "__main__":
    unittest.main()
