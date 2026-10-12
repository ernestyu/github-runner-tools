#!/usr/bin/env python3
"""Fresh-install packaging, identity and legacy-removal contracts."""
from __future__ import annotations

import hashlib
import io
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

    def _build_release(self, td: str, version: str = "v9.9.9"):
        out = Path(td) / "dist"
        proc = subprocess.run(
            ["bash", str(ROOT / "scripts/build-release.sh"), version, str(out)],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out

    def _fake_curl_path(self, td: str) -> Path:
        bindir = Path(td) / "fake-bin"
        bindir.mkdir(exist_ok=True)
        curl = bindir / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "out=''\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  if [[ $1 == -o ]]; then out=$2; shift 2; else shift; fi\n"
            "done\n"
            "[[ -n $out ]] || exit 2\n"
            "[[ -f ${FAKE_CURL_SOURCE:-} ]] || { echo fake-curl-missing >&2; exit 22; }\n"
            "cp -- \"$FAKE_CURL_SOURCE\" \"$out\"\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        # Verified bootstrap is exercised without any chance of privileged
        # host mutation, independently of the CI runner's sudo permissions.
        sudo = bindir / "sudo"
        sudo.write_text("#!/bin/sh\nexit 73\n", encoding="utf-8")
        sudo.chmod(0o755)
        return bindir

    def _run_generated_bootstrap(self, bootstrap: Path, source: Path, work: Path):
        env = os.environ.copy()
        env["FAKE_CURL_SOURCE"] = str(source)
        env["PATH"] = str(self._fake_curl_path(str(work))) + os.pathsep + env["PATH"]
        return subprocess.run(
            ["bash", str(bootstrap)],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_generated_bootstrap_verifies_extracts_and_runs_outside_git(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = self._build_release(td)
            work = root / "not-a-checkout"
            work.mkdir()
            self.assertFalse((work / ".git").exists())
            result = self._run_generated_bootstrap(
                out / "install.sh",
                out / "github-runner-tools-v9.9.9.tar.gz",
                work,
            )
            # CI host is not a disposable fresh-install machine. The
            # verified entrypoint must reach preflight but never mutate host.
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(
                "systemd_required" in result.stderr or
                "sudo_required" in result.stderr,
                result.stderr,
            )
            self.assertNotIn("debian_13_required", result.stderr)
            self.assertNotIn("SHA-256 mismatch", result.stderr)
            self.assertNotIn("missing fresh installer", result.stderr)

    def test_generated_bootstrap_rejects_tampered_or_missing_payload_before_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = self._build_release(td)
            archive = out / "github-runner-tools-v9.9.9.tar.gz"
            tampered = root / "tampered.tar.gz"
            raw = bytearray(archive.read_bytes())
            raw[len(raw) // 2] ^= 1
            tampered.write_bytes(raw)
            work = root / "tampered-work"
            work.mkdir()
            result = self._run_generated_bootstrap(out / "install.sh", tampered, work)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release payload SHA-256 mismatch", result.stderr)
            self.assertNotIn("debian_13_required", result.stderr)

            missing = root / "does-not-exist.tar.gz"
            result = self._run_generated_bootstrap(out / "install.sh", missing, work)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("fake-curl-missing", result.stderr)
            self.assertNotIn("debian_13_required", result.stderr)

    def test_generated_bootstrap_rejects_verified_archive_without_fixed_entrypoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = self._build_release(td)
            malformed = root / "missing-entry.tar.gz"
            with tarfile.open(malformed, "w:gz") as handle:
                data = b"not an installer\n"
                info = tarfile.TarInfo("README")
                info.size = len(data)
                handle.addfile(info, io.BytesIO(data))
            digest = hashlib.sha256(malformed.read_bytes()).hexdigest()
            bootstrap = root / "install-missing-entry.sh"
            text = (out / "install.sh").read_text(encoding="utf-8")
            text = text.replace(
                next(line for line in text.splitlines() if line.startswith("PAYLOAD_SHA256=")),
                f'PAYLOAD_SHA256="{digest}"',
            )
            bootstrap.write_text(text, encoding="utf-8")
            bootstrap.chmod(0o755)
            work = root / "missing-entry-work"
            work.mkdir()
            result = self._run_generated_bootstrap(bootstrap, malformed, work)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("verified payload missing fresh installer", result.stderr)
            self.assertNotIn("debian_13_required", result.stderr)

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

    def test_administrator_secret_never_enters_argv_or_environment(self):
        source = (ROOT / "scripts/fresh-install.sh").read_text()
        self.assertNotIn('python3 - "$password"', source)
        self.assertNotIn('sys.argv[1]', source)
        self.assertIn('sys.stdin.buffer.read(4097)', source)
        self.assertIn('printf \'%s\' "$password" |', source)
        self.assertNotIn('export password', source)
        self.assertNotIn('PASSWORD="$password"', source)
        self.assertNotIn('echo "$password"', source)
        self.assertIn('password= confirm=', source)

    def test_strict_install_state_parser_is_packaged(self):
        source = (ROOT / "scripts/install-state-validator.py").read_text()
        self.assertIn("object_pairs_hook=unique", source)
        self.assertIn("os.O_NOFOLLOW", source)
        self.assertIn("set(obj) != set(KEYS)", source)
        self.assertIn('type(obj["schema"]) is not int', source)
        self.assertIn('type(obj[k]) is not int', source)
        self.assertIn('st.st_nlink != 1', source)
        self.assertIn("ipaddress.IPv4Address", source)
        self.assertIn("uuid.UUID", source)
        self.assertIn("STAGES", source)
        self.assertIn("install-state-validator.py", (ROOT / "scripts/build-release.sh").read_text())

    def test_active_product_has_no_legacy_setup_or_dispatch_fallback(self):
        absent = (
            "scripts/setup-web-management.sh",
            "scripts/setup-web-management-v2.sh",
            "scripts/web-v2-cutover-rollback.sh",
            "scripts/rollback-web-management-v2.sh",
            "web/dispatcher.py",
            "web/lifecycle_worker.py",
            "docs/implementation/web-management-v2-architecture.md",
            "docs/specs/20261007-web-management-v1-spec.md",
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
