#!/usr/bin/env python3
"""Verify the fresh-release payload contains the complete current runtime."""
from __future__ import annotations

import pathlib
import subprocess
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class InstallManifestTests(unittest.TestCase):
    def build_payload(self, td: str):
        out = pathlib.Path(td) / "dist"
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "build-release.sh"), "v9.9.8", str(out)],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = out / "github-runner-tools-v9.9.8.tar.gz"
        with tarfile.open(archive, "r:gz") as handle:
            return {member.name.lstrip("./") for member in handle.getmembers()}

    def test_payload_contains_all_current_web_modules(self):
        with tempfile.TemporaryDirectory() as td:
            names = self.build_payload(td)
        source_modules = {f"web/{p.name}" for p in WEB.glob("*.py")}
        self.assertTrue(source_modules.issubset(names), source_modules - names)
        self.assertNotIn("web/dispatcher.py", names)
        self.assertNotIn("web/lifecycle_worker.py", names)

    def test_pinned_cli_and_launcher_paths_match_fresh_installer(self):
        contract = (WEB / "v2_executor_contract.py").read_text()
        spawn = (WEB / "v2_cli_spawn.py").read_text()
        provenance = (WEB / "v2_code_provenance.py").read_text()
        installer = (ROOT / "scripts" / "fresh-install.sh").read_text()
        target = "/usr/local/lib/github-runner-tools/cli"
        for name in ("register-runner.sh", "remove-runner.sh", "status-runners.sh"):
            with self.subTest(name=name):
                self.assertIn(target + "/" + name, contract)
                self.assertIn(name, installer)
        self.assertIn("/usr/local/lib/github-runner-tools/web/v2_cli_launcher.py", spawn)
        self.assertIn("/usr/local/lib/github-runner-tools/web/identity_drop.py", provenance)
        self.assertNotIn("lifecycle_worker.py", provenance)
        self.assertIn('CLI_ROOT="$INSTALL_ROOT/cli"', installer)
        self.assertIn('WEB_ROOT="$INSTALL_ROOT/web"', installer)

    def test_fresh_installer_has_no_git_or_migration_dependency(self):
        source = (ROOT / "scripts" / "fresh-install.sh").read_text().lower()
        self.assertNotIn("git -c", source)
        self.assertNotIn("rev-parse", source)
        self.assertNotIn("expected-sha", source)
        self.assertNotIn("setup-web-management", source)
        self.assertNotIn("cutover", source)
        self.assertNotIn("dispatcher.service", source)
        self.assertNotIn("tailscale", source)


if __name__ == "__main__":
    unittest.main()
