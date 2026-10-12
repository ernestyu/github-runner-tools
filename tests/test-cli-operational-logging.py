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

    def test_remediation_reason_allowlist_is_fixed(self):
        reasons = {
            "runner_service_entrypoint_missing",
            "runner_service_entrypoint_invalid",
            "unit_target_exists",
            "unit_stage_create_failed",
            "unit_publish_failed",
            "unit_daemon_reload_failed",
            "unit_enable_failed",
            "authority_state_rejected",
            "authority_identity_rejected",
            "authority_install_failed",
        }
        self.assertTrue(reasons.issubset(mod.REASONS))
        for reason in reasons:
            with self.subTest(reason=reason):
                record = mod.event(self.args(
                    event="service_install",
                    stage="REGISTERED_UNIT_INCOMPLETE",
                    result="failed",
                    reason=reason,
                    exit_code=1,
                ))
                self.assertEqual(record["reason"], reason)

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

    def test_corrupt_tail_not_truncated(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / "operational.lock").touch(mode=0o600)
            (p / "operational.jsonl").write_bytes(b'{"v":1')
            for name in ("operational.lock", "operational.jsonl"):
                os.chmod(p / name, 0o600)
            with self.assertRaises(mod.LoggingUnavailable):
                with mod.Logger(root):
                    pass
            self.assertEqual((p / "operational.jsonl").read_bytes(), b'{"v":1')

    def test_hardlink_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / "operational.lock").touch(mode=0o600)
            os.chmod(p / "operational.lock", 0o600)
            (p / "operational.jsonl").write_text('{"v":1}\n')
            os.chmod(p / "operational.jsonl", 0o600)
            os.link(p / "operational.jsonl", p / "alias")
            with self.assertRaises(mod.LoggingUnavailable):
                with mod.Logger(root):
                    pass

    def test_capacity_evicts_oldest_closed(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / "operational.lock").touch(mode=0o600)
            os.chmod(p / "operational.lock", 0o600)
            old_max = mod.MAX_TOTAL
            mod.MAX_TOTAL = 80
            try:
                before = dt.datetime.now(dt.timezone.utc)
                for i, minutes in enumerate((3, 2, 1)):
                    stamp=(before-dt.timedelta(minutes=minutes)).strftime("%Y%m%dT%H%M%S%fZ")
                    name=p / f"operational.{stamp}.{i:032x}.jsonl"
                    name.write_text('{"v":1}\n' * 6)
                    os.chmod(name, 0o600)
                with mod.Logger(root):
                    pass
                self.assertLessEqual(sum(v.stat().st_size for v in p.iterdir()), mod.MAX_TOTAL)
                self.assertFalse(any(n.name.endswith(f"{0:032x}.jsonl") for n in p.iterdir()))
            finally:
                mod.MAX_TOTAL = old_max

    def test_expired_active_is_pruned_on_next_invocation(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / "operational.lock").touch(mode=0o600)
            os.chmod(p / "operational.lock", 0o600)
            active=p / "operational.jsonl"
            active.write_text('{"v":1}\n')
            os.chmod(active, 0o600)
            old=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=20)).timestamp()
            os.utime(active, (old, old))
            with mod.Logger(root):
                pass
            self.assertFalse(any(n.name.endswith(".jsonl") for n in p.iterdir()))

    def test_untrusted_free_text_not_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)
            (p / "operational.lock").touch(mode=0o600)
            os.chmod(p / "operational.lock", 0o600)
            attacker="ghs_FAKE_TOKEN_secret" * 20
            with self.assertRaises(mod.LoggingUnavailable):
                mod.event(self.args(reason=attacker))
            with mod.Logger(root) as log:
                log.append(mod.event(self.args()))
            contents=(p / "operational.jsonl").read_text()
            self.assertNotIn("FAKE_TOKEN", contents)
            self.assertEqual(len(contents.splitlines()),1)

if __name__ == "__main__":
    unittest.main()
