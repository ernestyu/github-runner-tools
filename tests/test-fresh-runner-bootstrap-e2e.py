#!/usr/bin/env python3
"""B-02 fresh-runner service bootstrap E2E.

Root-isolated lifecycle state, disposable canonical runner directories, and
ephemeral canonical Units are used. No GitHub API, production runner, or
production lifecycle state is touched.

Each tested version begins from the pinned official GitHub Runner release
archive, verifies the frozen SHA-256, and performs a normal tar extraction.
Only the registration network result is replaced by the frozen SPEC's
permitted isolated post-config fixture (.runner/.credentials). Root-level
runsvc.sh is asserted absent immediately before the production bootstrap.

Both direct CLI Authority and Web/Executor Runtime paths exercise the real
production bootstrap, canonical Unit validation, start, and health sequence.
"""
from __future__ import annotations

import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))

import cli_create_authority as cli
import dispatcher
from grt_web_common import canonical_service_name, make_local_id
import runner_lifecycle_authority as auth
import v2_unit_installer as installer

FIXTURES = ROOT / "tests" / "fixtures"
VERSIONS = ("2.328.0", "2.338.0")
ARCHIVES = {
    "2.328.0": {
        "url": "https://github.com/actions/runner/releases/download/v2.328.0/actions-runner-linux-x64-2.328.0.tar.gz",
        "sha256": "01066fad3a2893e63e6ca880ae3a1fad5bf9329d60e77ee15f2b97c148c3cd4e",
    },
    "2.338.0": {
        "url": "https://github.com/actions/runner/releases/download/v2.338.0/actions-runner-linux-x64-2.338.0.tar.gz",
        "sha256": "af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32",
    },
}


@unittest.skipUnless(os.geteuid() == 0, "root-only isolated fresh-runner E2E")
class FreshRunnerBootstrapE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sudo_user = os.environ.get("SUDO_USER")
        if not sudo_user or sudo_user == "root":
            raise unittest.SkipTest(
                "requires sudo from a non-root CI runner account")
        cls.archive_tmp = tempfile.TemporaryDirectory(
            prefix="grt-b02-official-archives-")
        cls.archive_root = Path(cls.archive_tmp.name)
        cls.archives: dict[str, Path] = {}
        for version in VERSIONS:
            meta = ARCHIVES[version]
            archive = cls.archive_root / (
                f"actions-runner-linux-x64-{version}.tar.gz")
            digest = hashlib.sha256()
            with urllib.request.urlopen(meta["url"], timeout=120) as response, \
                    archive.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
            if digest.hexdigest() != meta["sha256"]:
                raise AssertionError(
                    f"official Runner archive digest mismatch for {version}")
            cls.archives[version] = archive

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "archive_tmp"):
            cls.archive_tmp.cleanup()

    def setUp(self):
        sudo_user = os.environ.get("SUDO_USER")
        if not sudo_user or sudo_user == "root":
            self.skipTest("requires sudo from a non-root CI runner account")
        self.user = sudo_user
        self.account = pwd.getpwnam(self.user)
        self.group = grp.getgrgid(self.account.pw_gid).gr_name
        self.home = Path(self.account.pw_dir)

        self.sandbox = tempfile.TemporaryDirectory(prefix="grt-b02-fresh-e2e-")
        self.addCleanup(self.sandbox.cleanup)
        self.base = Path(self.sandbox.name)
        self.old_root = auth.ROOT
        auth.ROOT = str(self.base / "authority-state")
        self.addCleanup(setattr, auth, "ROOT", self.old_root)

        self.created_units: list[Path] = []
        self.created_dirs: list[Path] = []
        self.addCleanup(self._cleanup_host_artifacts)

    def _cleanup_host_artifacts(self):
        for unit in reversed(self.created_units):
            try:
                unit.unlink()
            except FileNotFoundError:
                pass
        for runner in reversed(self.created_dirs):
            shutil.rmtree(runner, ignore_errors=True)

    @staticmethod
    def _service_props(service: str, runner_dir: Path, user: str) -> str:
        unit = f"/etc/systemd/system/{service}"
        runsvc = runner_dir / "runsvc.sh"
        return "\n".join([
            "LoadState=loaded",
            f"FragmentPath={unit}",
            "DropInPaths=",
            f"User={user}",
            f"WorkingDirectory={runner_dir}",
            f"ExecStart={{ path={runsvc} ; argv[]={runsvc} ; }}",
            "ExecStartPre=",
            "ExecStartPost=",
            "ExecStop=",
            "ExecStopPost=",
            "ExecReload=",
        ]) + "\n"

    def _fake_systemd_run(self, service: str, runner_dir: Path):
        props = self._service_props(service, runner_dir, self.user)

        def run(cmd, *args, **kwargs):
            cmd = [str(x) for x in cmd]
            if len(cmd) >= 2 and cmd[1] == "show":
                return SimpleNamespace(returncode=0, stdout=props, stderr="")
            if len(cmd) >= 2 and cmd[1] in ("daemon-reload", "enable"):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected installer/CLI systemctl call: {cmd!r}")

        return run

    def _runtime_systemd(self, service: str, runner_dir: Path):
        props = self._service_props(service, runner_dir, self.user)
        calls: list[tuple[str, ...]] = []

        def run(cmd, *, deadline=None, **kwargs):
            cmd = tuple(str(x) for x in cmd)
            calls.append(cmd)
            if len(cmd) >= 2 and cmd[1] == "show":
                return SimpleNamespace(returncode=0, stdout=props, stderr="")
            if len(cmd) >= 2 and cmd[1] == "is-active":
                return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
            if len(cmd) >= 2 and cmd[1] == "start":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected Runtime systemctl call: {cmd!r}")

        return run, calls

    def _fresh_post_config(self, version: str, mode: str):
        suffix = version.replace(".", "")
        repository = f"fixture/b02-{mode}-{suffix}-{os.getpid()}"
        runner_name = f"b02-{mode}-{suffix}-{os.getpid()}"
        owner, project = repository.split("/", 1)
        runner_dir = self.home / (
            "actions-runner-" + make_local_id(owner, project))
        if runner_dir.exists() or runner_dir.is_symlink():
            self.fail(
                f"disposable runner directory unexpectedly exists: {runner_dir}")
        runner_dir.mkdir(mode=0o700)
        self.created_dirs.append(runner_dir)

        # Frozen SPEC §4.6 #1/#2: begin from the pinned official release
        # archive and perform a normal tar extraction into an empty runner dir.
        archive = self.archives[version]
        extracted = subprocess.run(
            ["tar", "-xzf", str(archive), "-C", str(runner_dir)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(extracted.returncode, 0, extracted.stderr)

        bindir = runner_dir / "bin"
        wrapper = bindir / "runsvc.sh"
        service_template = bindir / "actions.runner.service.template"
        svc = runner_dir / "svc.sh"
        config = runner_dir / "config.sh"

        self.assertTrue(wrapper.is_file())
        self.assertTrue(service_template.is_file())
        self.assertTrue(svc.is_file())
        self.assertTrue(config.is_file())

        # Cross-check the two bootstrap-critical files against the audited
        # upstream Git blobs in addition to the whole-archive SHA-256 pin.
        wrapper_fixture = FIXTURES / f"actions-runner-v{version}-runsvc.sh"
        service_template_fixture = (
            FIXTURES /
            f"actions-runner-v{version}-actions.runner.service.template")
        self.assertEqual(wrapper.read_bytes(), wrapper_fixture.read_bytes())
        self.assertEqual(
            service_template.read_bytes(),
            service_template_fixture.read_bytes())

        service = canonical_service_name(repository, runner_name)

        # Frozen SPEC permits a faithful isolated registration fixture instead
        # of a real GitHub registration. Only registration outputs are
        # synthesized; archive-origin files remain exactly as extracted.
        (runner_dir / ".runner").write_text(json.dumps({
            "agentName": runner_name,
            "agentId": 100000 + os.getpid(),
            "gitHubUrl": "https://github.com/" + repository,
        }, separators=(",", ":")))
        (runner_dir / ".credentials").write_text(
            "SYNTHETIC_E2E_CREDENTIAL_RECORD\n")
        for name in (".runner", ".credentials"):
            (runner_dir / name).chmod(0o600)

        # Production extraction is performed by the runner account. The E2E
        # itself is root-isolated, so normalize ownership after the otherwise
        # ordinary tar extraction without altering archive file contents/modes.
        for root, dirs, files in os.walk(runner_dir):
            os.chown(root, self.account.pw_uid, self.account.pw_gid)
            for name in dirs:
                os.chown(Path(root) / name,
                         self.account.pw_uid, self.account.pw_gid)
            for name in files:
                os.chown(Path(root) / name,
                         self.account.pw_uid, self.account.pw_gid)

        # Live-regression preconditions, now proven from a real official
        # extraction rather than a hand-built archive-layout fixture.
        self.assertFalse((runner_dir / "runsvc.sh").exists())
        self.assertFalse((runner_dir / ".service").exists())
        unit = Path("/etc/systemd/system") / service
        self.assertFalse(unit.exists() or unit.is_symlink())
        self.created_units.append(unit)
        return repository, runner_name, runner_dir, service, wrapper_fixture

    def _prepare_authority_cycle(self, repository, runner_name, runner_dir, version):
        auth.create_stage(repository, runner_name, str(runner_dir),
                          "PRE_REGISTRATION")
        auth.create_stage(repository, runner_name, str(runner_dir),
                          "REGISTERED_PERMISSION_INCOMPLETE")
        auth.normalize_new_registration(
            repository, runner_name, str(runner_dir), self.user)
        auth.create_attestation(
            repository, runner_name, str(runner_dir), self.user, version)
        auth.create_stage(repository, runner_name, str(runner_dir),
                          "REGISTERED_UNIT_INCOMPLETE")

    def _assert_terminal_create(self, repository, runner_name, runner_dir):
        current = auth._current(repository, runner_name, str(runner_dir))
        self.assertIsNotNone(current)
        created = auth._cycle_record(
            auth.CREATE_STATES, repository, runner_name, str(runner_dir),
            current["instance"])
        self.assertEqual(created["stage"], "CREATE_COMPLETE")
        unit_att = auth._cycle_record(
            auth.UNIT_ATTESTATIONS, repository, runner_name, str(runner_dir),
            current["instance"])
        self.assertIsNotNone(unit_att)

    def test_direct_cli_fresh_bootstrap_start_health_both_versions(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                repository, runner_name, runner_dir, service, fixture = (
                    self._fresh_post_config(version, "cli"))
                self._prepare_authority_cycle(
                    repository, runner_name, runner_dir, version)

                # Production trusted CLI Authority -> production installer.
                fake_run = self._fake_systemd_run(service, runner_dir)
                with mock.patch.dict(os.environ, {"SUDO_USER": self.user}), \
                     mock.patch.object(installer.subprocess, "run",
                                       side_effect=fake_run):
                    cli.main([
                        "install", repository, runner_name, str(runner_dir),
                        "-", version,
                    ])

                    self.assertEqual(
                        (runner_dir / "runsvc.sh").read_bytes(),
                        fixture.read_bytes())
                    self.assertEqual(
                        (runner_dir / "runsvc.sh").stat().st_mode & 0o777,
                        0o755)
                    self.assertEqual(
                        (runner_dir / "runsvc.sh").stat().st_uid,
                        self.account.pw_uid)
                    self.assertTrue(self.created_units[-1].is_file())
                    self.assertEqual(
                        self.created_units[-1].stat().st_mode & 0o777, 0o644)

                    # Production CLI writes .service after trusted install,
                    # then asks the same Authority to attest the Unit.
                    (runner_dir / ".service").write_text(service + "\n")
                    os.chown(runner_dir / ".service",
                             self.account.pw_uid, self.account.pw_gid)
                    cli.main([
                        "unit", repository, runner_name, str(runner_dir),
                        "-", "-",
                    ])

                auth.create_stage(repository, runner_name, str(runner_dir),
                                  "REGISTERED_START_INCOMPLETE")

                # Exercise the official post-config svc.sh start/status surface
                # used by direct CLI Create, with an isolated fake systemctl.
                fakebin = self.base / f"fakebin-{version.replace('.', '')}"
                fakebin.mkdir(exist_ok=True)
                systemctl_log = self.base / f"systemctl-{version}.log"
                fake_systemctl = fakebin / "systemctl"
                fake_systemctl.write_text(
                    "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$GRT_SYSTEMCTL_LOG\"\n"
                    "exit 0\n")
                fake_systemctl.chmod(0o755)
                env = dict(os.environ)
                env["PATH"] = str(fakebin) + ":" + env.get("PATH", "")
                env["GRT_SYSTEMCTL_LOG"] = str(systemctl_log)
                for command in ("start", "status"):
                    proc = subprocess.run(
                        ["./svc.sh", command], cwd=runner_dir, env=env,
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(proc.returncode, 0, proc.stderr)

                log = systemctl_log.read_text()
                self.assertIn(f"start {service}", log)
                self.assertIn(f"--no-pager status {service}", log)

                auth.create_stage(repository, runner_name, str(runner_dir),
                                  "REGISTERED_HEALTH_UNKNOWN")
                auth.create_stage(repository, runner_name, str(runner_dir),
                                  "CREATE_COMPLETE")
                self._assert_terminal_create(
                    repository, runner_name, runner_dir)

    def test_executor_fresh_bootstrap_start_health_both_versions(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                repository, runner_name, runner_dir, service, fixture = (
                    self._fresh_post_config(version, "web"))

                runtime = object.__new__(dispatcher.Runtime)
                runtime.runner_user = self.user
                runtime.runner_group = self.group
                runtime.runner_home = str(self.home)
                runtime.timeout = 60

                run, calls = self._runtime_systemd(service, runner_dir)
                runtime._run = run

                common = {
                    "repository": repository,
                    "runner_dir": str(runner_dir),
                    "runner_name": runner_name,
                }
                self.assertTrue(runtime.privileged({
                    **common, "op": "create_state",
                    "stage": "PRE_REGISTRATION"})["ok"])

                # Registration fixture exists at this point; execute the real
                # Executor permission/attestation/state authority sequence.
                self.assertTrue(runtime.privileged({
                    **common, "op": "create_state",
                    "stage": "REGISTERED_PERMISSION_INCOMPLETE"})["ok"])
                self.assertTrue(runtime.privileged({
                    **common, "op": "registration_permission_normalize"})["ok"])
                self.assertTrue(runtime.privileged({
                    **common, "op": "registration_attestation_create",
                    "version": version})["ok"])
                self.assertTrue(runtime.privileged({
                    **common, "op": "create_state",
                    "stage": "REGISTERED_UNIT_INCOMPLETE"})["ok"])

                self.assertFalse((runner_dir / "runsvc.sh").exists())
                fake_install_run = self._fake_systemd_run(service, runner_dir)
                with mock.patch.object(
                        installer.subprocess, "run",
                        side_effect=fake_install_run):
                    installed = runtime.privileged({
                        **common, "op": "service_install",
                        "version": version})
                self.assertEqual(
                    installed, {"ok": True, "service": service})
                self.assertEqual(
                    (runner_dir / "runsvc.sh").read_bytes(),
                    fixture.read_bytes())

                (runner_dir / ".service").write_text(service + "\n")
                os.chown(runner_dir / ".service",
                         self.account.pw_uid, self.account.pw_gid)

                self.assertTrue(runtime.privileged({
                    **common, "op": "create_state",
                    "stage": "REGISTERED_START_INCOMPLETE"})["ok"])
                self.assertTrue(runtime.privileged({
                    **common, "op": "service_start",
                    "service": service})["ok"])
                self.assertTrue(runtime.privileged({
                    **common, "op": "create_state",
                    "stage": "REGISTERED_HEALTH_UNKNOWN"})["ok"])
                health = runtime.privileged({
                    **common, "op": "service_state",
                    "service": service})
                self.assertEqual(health, {"ok": True, "state": "active"})
                self.assertTrue(runtime.privileged({
                    **common, "op": "create_state",
                    "stage": "CREATE_COMPLETE"})["ok"])

                self.assertTrue(any(
                    len(cmd) >= 2 and cmd[1] == "start" for cmd in calls))
                self.assertTrue(any(
                    len(cmd) >= 2 and cmd[1] == "is-active" for cmd in calls))
                self._assert_terminal_create(
                    repository, runner_name, runner_dir)


if __name__ == "__main__":
    unittest.main()
