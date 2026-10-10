#!/usr/bin/env python3
"""Pinned actions/runner v2.328.0 systemd.svc.sh.template offline contract."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "tests/fixtures/actions-runner-v2.328.0-systemd.svc.sh.template"
BLOB = "8429854293708c0657822e5625cb7f2b61903871"
UNIT = "actions.runner.example-repo.fixture.service"


class OfficialRunnerCompatibility(unittest.TestCase):
    def test_upstream_pinned_blob_and_contract(self):
        raw = UPSTREAM.read_bytes()
        self.assertEqual(hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(), BLOB)
        lines = UPSTREAM.read_text().splitlines()
        self.assertTrue(any("CONFIG_PATH=.service" in x for x in lines))
        self.assertTrue(any('rm "$' + '{CONFIG_PATH}"' in x for x in lines))
        self.assertTrue(any('rm "$' + '{UNIT_PATH}"' in x for x in lines))
        self.assertTrue(any('chmod 664 "$' + '{UNIT_PATH}"' in x for x in lines))

    def test_real_uninstall_side_effects_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "runner"
            root.mkdir()
            (root / "bin").mkdir()
            (root / "bin/actions.runner.service.template").write_text("[Service]\n")
            (root / ".service").write_text(UNIT + "\n")
            (root / ".runner").write_text('{"agentName":"fixture","gitHubUrl":"https://github.com/example/repo"}')
            (root / ".credentials").write_text("SYNTHETIC_ONLY")
            unit = base / "fake-unit"
            unit.write_text("[Unit]\n")
            shim = base / "shims"
            shim.mkdir()
            calls = base / "calls"
            (shim / "id").write_text('#!/bin/sh\nif [ "$1" = "-u" ]; then echo 0; else /usr/bin/id "$@"; fi\n')
            (shim / "systemctl").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GRT_TEST_SYSTEMCTL_LOG"\nexit 0\n')
            for name in ("id", "systemctl"):
                (shim / name).chmod(0o755)
            original = "UNIT_PATH=/etc/systemd/system/$" + "{SVC_NAME}"
            source = UPSTREAM.read_text()
            self.assertIn(original, source)
            script = source.replace(original, 'UNIT_PATH="$' + '{GRT_TEST_UNIT_PATH}"')
            script = script.replace("{{SvcNameVar}}", UNIT).replace("{{SvcDescription}}", "fixture")
            (root / "svc.sh").write_text(script)
            env = {**os.environ, "PATH": str(shim) + ":" + os.environ.get("PATH", ""),
                   "GRT_TEST_UNIT_PATH": str(unit), "GRT_TEST_SYSTEMCTL_LOG": str(calls),
                   "GITHUB_ACTIONS_RUNNER_SERVICE_TEMPLATE": ""}
            proc = subprocess.run(["bash", "./svc.sh", "uninstall"], cwd=root, env=env,
                                  capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(unit.exists())
            self.assertFalse((root / ".service").exists())
            self.assertTrue((root / ".runner").exists())
            self.assertTrue((root / ".credentials").exists())
            log = calls.read_text()
            self.assertIn("stop " + UNIT, log)
            self.assertIn("disable " + UNIT, log)
            self.assertIn("daemon-reload", log)


if __name__ == "__main__":
    unittest.main()
