#!/usr/bin/env python3
"""Verify the V2 installer includes every root-owned local Python dependency."""
import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SETUP = (ROOT / "scripts" / "setup-web-management-v2.sh").read_text()


class InstallManifestTests(unittest.TestCase):
    def test_installed_module_import_closure(self):
        all_modules = {p.stem for p in WEB.glob("*.py")}
        # Bound the extraction to the fixed source-install for-list.
        block = SETUP.split("for file in v2_executor_contract.py", 1)[1].split("; do", 1)[0]
        installed = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*\.py", "v2_executor_contract.py" + block))
        for filename in sorted(installed):
            with self.subTest(filename=filename):
                source = WEB / filename
                self.assertTrue(source.is_file(), f"missing {filename}")
                tree = ast.parse(source.read_text())
                local = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        local.update(alias.name.split(".", 1)[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        local.add(node.module.split(".", 1)[0])
                missing = {name + ".py" for name in (local & all_modules)} - installed
                self.assertFalse(missing, f"{filename} imports uninstalled modules: {missing}")

    def test_pinned_cli_and_launcher_paths_match_frozen_spec(self):
        contract = (WEB / "v2_executor_contract.py").read_text()
        spawn = (WEB / "v2_cli_spawn.py").read_text()
        target = "/usr/local/lib/github-runner-tools/cli"
        for name in ("register-runner.sh", "remove-runner.sh", "status-runners.sh"):
            with self.subTest(name=name):
                self.assertIn(target + "/" + name, contract)
        self.assertIn("/usr/local/lib/github-runner-tools/web/v2_cli_launcher.py", spawn)
        self.assertIn('CLI_ROOT="/usr/local/lib/github-runner-tools/cli"', SETUP)
        self.assertIn('"$CLI_ROOT/$file"', SETUP)
        self.assertNotIn('"$INSTALL_ROOT/cli/$file"', SETUP)

    def test_installer_never_runs_without_explicit_sha(self):
        self.assertIn("--expected-sha", SETUP)
        self.assertIn("V2_EXECUTOR_MODE=1", SETUP)
        self.assertIn("lifecycle-snapshot", SETUP)

if __name__ == "__main__":
    unittest.main()
