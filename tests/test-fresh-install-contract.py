#!/usr/bin/env python3
"""Fresh-install packaging, identity and legacy-removal contracts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FreshInstallContractTests(unittest.TestCase):
    def test_release_payload_is_minimal_git_free_and_digest_bound(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "dist"
            proc = subprocess.run(
                ["bash", str(ROOT / "scripts/build-release.sh"), "v9.9.9", str(out)],
                cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            archive = out / "github-runner-tools-v9.9.9.tar.gz"
            bootstrap = out / "install.sh"
            sums = (out / "SHA256SUMS").read_text().splitlines()
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(sums[0], f"{digest}  {archive.name}")

            text = bootstrap.read_text()
            self.assertIn('VERSION="v9.9.9"', text)
            self.assertIn(f'PAYLOAD_SHA256="{digest}"', text)
            self.assertIn(
                "releases/download/v9.9.9/github-runner-tools-v9.9.9.tar.gz",
                text)
            self.assertNotIn("@GRT_VERSION@", text)
            self.assertNotIn("@GRT_SHA256@", text)

            with tarfile.open(archive, "r:gz") as tf:
                names = {m.name.lstrip("./") for m in tf.getmembers()}
            required = {
                "scripts/fresh-install.sh",
                "scripts/install-lan.sh",
                "scripts/setup-local-archive.sh",
                "scripts/register-runner.sh",
                "scripts/remove-runner.sh",
                "scripts/status-runners.sh",
                "web/app.py",
                "web/v2_executor.py",
                "hooks/archive-job-completed.sh",
            }
            self.assertTrue(required.issubset(names), required - names)
            self.assertFalse(any(
                n == ".git" or n.startswith(".git/") or
                n.startswith("tests/") or n.startswith("docs/") or
                n.startswith(".github/")
                for n in names
            ))

            tampered = bytearray(archive.read_bytes())
            tampered[len(tampered) // 2] ^= 1
            self.assertNotEqual(
                hashlib.sha256(tampered).hexdigest(),
                digest,
            )

    def test_root_invocation_rejected_before_persistent_mutation(self):
        if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
            self.skipTest("passwordless sudo unavailable")
        watched = [
            Path("/usr/local/lib/github-runner-tools"),
            Path("/etc/github-runner-tools"),
            Path("/var/lib/github-runner-tools"),
            Path("/var/log/github-runner-tools"),
            Path("/etc/systemd/system/github-runner-tools-executor.service"),
            Path("/etc/systemd/system/github-runner-tools-web.service"),
        ]
        before = [(p.exists(), p.is_symlink()) for p in watched]
        proc = subprocess.run(
            ["sudo", "-n", "bash", str(ROOT / "scripts/fresh-install.sh")],
            cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("root_invocation_forbidden", proc.stderr)
        after = [(p.exists(), p.is_symlink()) for p in watched]
        self.assertEqual(before, after)

    def test_install_attempt_contract_is_finite_and_fail_closed(self):
        source = (ROOT / "scripts/fresh-install.sh").read_text()
        stages = (
            "ATTEMPT_STARTED",
            "RUNTIME_INSTALLED",
            "AUTHORITY_STATE_INITIALIZED",
            "ARCHIVE_PLATFORM_INITIALIZED",
            "EXECUTOR_INSTALLED",
            "WEB_INSTALLED",
            "SERVICES_STARTED",
            "SMOKE_VERIFIED",
            "INSTALL_COMPLETE",
        )
        for stage in stages:
            self.assertIn(stage, source)
        for binding in (
            "partial_release_mismatch",
            "partial_payload_mismatch",
            "partial_runner_user_mismatch",
            "partial_runner_uid_mismatch",
            "partial_runner_gid_mismatch",
            "partial_runner_home_mismatch",
            "untracked_partial_installation",
            "installation_already_complete",
            "unknown_install_stage",
        ):
            self.assertIn(binding, source)
        self.assertIn('INSTALL_STATE="$STATE_ROOT/install-state.json"', source)
        self.assertIn("GRT_TEST_FAULT_STAGE", source)
        self.assertIn("configured_lan_address_not_assigned", source)

    def test_active_product_has_no_legacy_setup_or_dispatch_fallback(self):
        absent = (
            "scripts/setup-web-management.sh",
            "scripts/setup-web-management-v2.sh",
            "scripts/web-v2-cutover-rollback.sh",
            "scripts/rollback-web-management-v2.sh",
        )
        for rel in absent:
            self.assertFalse((ROOT / rel).exists(), rel)
        app = (ROOT / "web/app.py").read_text()
        self.assertNotIn("V2_EXECUTOR_MODE", app)
        self.assertNotIn("web-dispatch.sock", app)
        self.assertNotIn("tailscale", app.lower())
        fresh = (ROOT / "scripts/fresh-install.sh").read_text().lower()
        self.assertNotIn("tailscale", fresh)
        self.assertNotIn("github-runner-tools-dispatch.service", fresh)


if __name__ == "__main__":
    unittest.main()
