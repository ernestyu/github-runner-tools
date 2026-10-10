#!/usr/bin/env python3
"""Run with root to test the actual root-only journal/attestation authority.

The entire authority root is redirected to a fresh private /tmp sandbox.
No production data, systemd Unit, GitHub API or Runner is touched.
"""
import json
import os
from pathlib import Path
import pwd
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
import runner_lifecycle_authority as auth
import cli_create_authority as cli

REPO = "example/repo"
RUNNER = "fixture"


@unittest.skipUnless(os.geteuid() == 0, "root-only isolation fixture")
class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.base = Path(self.sandbox.name)
        self.previous = auth.ROOT
        auth.ROOT = str(self.base / "state")
        self.addCleanup(setattr, auth, "ROOT", self.previous)
        self.runner = self.base / "actions-runner-example--repo"
        self.runner.mkdir()
        self.repo = str(self.runner)
        (self.runner / ".runner").write_text(json.dumps({
            "agentName": RUNNER, "gitHubUrl": "https://github.com/example/repo"}))
        (self.runner / ".credentials").write_text("SYNTHETIC_NOT_SECRET")
        for name in (".runner", ".credentials"):
            (self.runner / name).chmod(0o600)

    def complete_create(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
        for stage in auth.STAGES[2:]:
            auth.create_stage(REPO, RUNNER, self.repo, stage)

    def test_full_stage_transitions_and_restart_block(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_UNIT_INCOMPLETE")
        for stage in auth.STAGES[1:]:
            if stage == "REGISTERED_UNIT_INCOMPLETE":
                auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
            auth.create_stage(REPO, RUNNER, self.repo, stage)
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        ledger = list((self.base / "state" / "create-state").glob("*.*.json"))
        self.assertEqual(len(ledger), 1)
        self.assertEqual(json.loads(ledger[0].read_text())["stage"], "CREATE_COMPLETE")
        self.assertEqual(ledger[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn("SYNTHETIC_NOT_SECRET", ledger[0].read_text())

    def test_uncertain_registration_is_terminal(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTRATION_OUTCOME_UNKNOWN")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")

    def test_root_only_attestation_contains_digests_not_credentials(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        auth.create_attestation(REPO, RUNNER, self.repo, pwd.getpwuid(os.getuid()).pw_name, "2.328.0")
        ledger = list((self.base / "state" / "runner-attestations").glob("*.json"))
        self.assertEqual(len(ledger), 1)
        payload = ledger[0].read_text()
        self.assertNotIn("SYNTHETIC_NOT_SECRET", payload)
        entry = json.loads(payload)
        self.assertEqual(entry["repository"], REPO)
        self.assertEqual(entry["runner"], RUNNER)
        self.assertEqual(entry["capture_stage"], "registered-secure-before-service")
        self.assertEqual(set(entry["files"]), {".runner", ".credentials"})
        self.assertEqual(ledger[0].stat().st_mode & 0o777, 0o600)
        with self.assertRaises(auth.AuthorityError):
            auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")

    def test_attestation_rejects_insecure_or_symlinked_files(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        credentials = self.runner / ".credentials"
        credentials.chmod(0o664)
        with self.assertRaises(auth.AuthorityError):
            auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
        credentials.chmod(0o600)
        credentials.unlink()
        credentials.symlink_to(self.runner / ".runner")
        with self.assertRaises((auth.AuthorityError, OSError)):
            auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
        self.assertFalse((self.base / "state" / "runner-attestations").exists()
                         and list((self.base / "state" / "runner-attestations").glob("*.json")))

    def test_dispatcher_unit_provenance_is_root_owned_and_mode_pinned(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_UNIT_INCOMPLETE")
        service = "actions.runner.example-repo.fixture.service"
        pretend_systemd = self.base / "pretend-systemd"
        pretend_systemd.mkdir()
        (pretend_systemd / service).write_text("[Service]\\nUser=root\\n")
        (pretend_systemd / service).chmod(0o644)
        original = auth._safe_directory

        def route(path):
            if path == "/etc/systemd/system":
                return os.open(pretend_systemd, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            return original(path)

        with mock.patch.object(auth, "_safe_directory", side_effect=route):
            auth.create_unit_attestation(REPO, RUNNER, self.repo, service,
                                         "/etc/systemd/system/" + service)
        proofs = list((self.base / "state" / "unit-attestations").glob("*.json"))
        self.assertEqual(len(proofs), 1)
        item = json.loads(proofs[0].read_text())
        self.assertEqual(item["unit"], service)
        self.assertEqual(item["mode"], "0644")
        self.assertEqual(proofs[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn("SYNTHETIC_NOT_SECRET", proofs[0].read_text())

    def test_initial_group_writable_unit_is_not_trusted(self):
        service = "actions.runner.example-repo.fixture.service"
        folder = self.base / "pretend-systemd"
        folder.mkdir()
        (folder / service).write_text("[Service]\\n")
        (folder / service).chmod(0o664)
        original = auth._safe_directory

        def route(path):
            if path == "/etc/systemd/system":
                return os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            return original(path)

        with mock.patch.object(auth, "_safe_directory", side_effect=route):
            with self.assertRaises(auth.AuthorityError):
                auth.create_unit_attestation(REPO, RUNNER, self.repo, service,
                                             "/etc/systemd/system/" + service)

    def test_remove_terminal_state_is_durable_and_nonretryable(self):
        self.complete_create()
        first = auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        self.assertEqual(first["inode"], self.runner.stat().st_ino)
        with self.assertRaises(auth.AuthorityError):
            auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        proof = auth.remove_stage(REPO, RUNNER, self.repo, "root", "CONFIRM_REMOTE_REMOVED")
        self.assertEqual(proof, first)
        with self.assertRaises(auth.AuthorityError):
            auth.remove_stage(REPO, RUNNER, self.repo, "root", "COMPLETE")
        shutil.rmtree(self.runner)
        self.assertEqual(auth.remove_stage(REPO, RUNNER, self.repo, "root", "COMPLETE"),
                         {"complete": True})
        with self.assertRaises(auth.AuthorityError):
            auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        states = list((self.base / "state" / "remove-state").glob("*.*.json"))
        self.assertEqual(len(states), 1)
        self.assertEqual(json.loads(states[0].read_text())["stage"], "REMOVE_COMPLETE")

    def test_remove_ledger_rejects_mismatched_metadata(self):
        self.complete_create()
        (self.runner / ".runner").write_text(json.dumps({
            "agentName": RUNNER, "gitHubUrl": "https://github.com/foreign/repo"}))
        with self.assertRaises(auth.AuthorityError):
            auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        self.assertFalse((self.base / "state" / "remove-state").exists()
                         and list((self.base / "state" / "remove-state").glob("*.json")))

    def test_strict_legacy_0600_remove_gets_isolated_non_attested_instance(self):
        record = self.runner / ".service"
        record.write_text("actions.runner.example-repo.fixture.service\n")
        record.chmod(0o644)
        original = auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        active = auth._current(REPO, RUNNER, self.repo)
        self.assertIsNotNone(active)
        creation = auth._cycle_record(auth.CREATE_STATES, REPO, RUNNER, self.repo,
                                      active["instance"])
        self.assertEqual(creation["stage"], "LEGACY_VERIFIED")
        self.assertEqual(creation["origin"], "legacy-strict-0600")
        self.assertIsNone(auth._cycle_record(
            auth.ATTESTATIONS, REPO, RUNNER, self.repo, active["instance"]))
        with self.assertRaises(auth.AuthorityError):
            auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        self.assertEqual(original["inode"], self.runner.stat().st_ino)

    def test_legacy_0664_cannot_receive_cleanup_cycle(self):
        service = self.runner / ".service"
        service.write_text("actions.runner.example-repo.fixture.service\n")
        service.chmod(0o644)
        (self.runner / ".credentials").chmod(0o664)
        with self.assertRaises(auth.AuthorityError):
            auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        self.assertIsNone(auth._current(REPO, RUNNER, self.repo))

    def test_create_crash_after_remote_side_effect_before_ack_blocks_retry(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        # Simulate a lost process after remote success, before the next record.
        active = auth._current(REPO, RUNNER, self.repo)
        proof = auth._cycle_record(auth.CREATE_STATES, REPO, RUNNER, self.repo, active["instance"])
        self.assertEqual(proof["stage"], "PRE_REGISTRATION")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTRATION_OUTCOME_UNKNOWN")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")

    def test_remove_crash_pending_before_confirm_never_repeats_remote(self):
        self.complete_create()
        first = auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        # Same durable state for both remote-failed and crashed-after-success.
        with self.assertRaises(auth.AuthorityError):
            auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        self.assertTrue(self.runner.exists())
        self.assertEqual(first["inode"], self.runner.stat().st_ino)

    def test_cleanup_pending_crash_preserves_instance_and_blocks_recreation(self):
        self.complete_create()
        auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        auth.remove_stage(REPO, RUNNER, self.repo, "root", "CONFIRM_REMOTE_REMOVED")
        with self.assertRaises(auth.AuthorityError):
            auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        state = auth._cycle_record(auth.REMOVE_STATES, REPO, RUNNER, self.repo,
                                   auth._current(REPO, RUNNER, self.repo)["instance"])
        self.assertEqual(state["stage"], "REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING")

    def test_fresh_create_only_normalizes_0664_registration(self):
        (self.runner / ".runner").chmod(0o664)
        (self.runner / ".credentials").chmod(0o664)
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        auth.normalize_new_registration(REPO, RUNNER, self.repo, "root")
        self.assertEqual((self.runner / ".runner").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.runner / ".credentials").stat().st_mode & 0o777, 0o600)
        auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_UNIT_INCOMPLETE")
        with self.assertRaises(auth.AuthorityError):
            auth.normalize_new_registration(REPO, RUNNER, self.repo, "root")

    def test_existing_untracked_0664_cannot_be_normalized(self):
        (self.runner / ".credentials").chmod(0o664)
        with self.assertRaises(auth.AuthorityError):
            auth.normalize_new_registration(REPO, RUNNER, self.repo, "root")
        self.assertEqual((self.runner / ".credentials").stat().st_mode & 0o777, 0o664)

    def test_cli_create_rejects_official_0664_unit_and_preserves_partial_stage(self):
        service = "actions.runner.example-repo.fixture.service"
        fake_systemd = self.base / "pretend-unit-root"
        fake_systemd.mkdir()
        unit = fake_systemd / service
        unit.write_text("[Service]\nUser=fixture\nWorkingDirectory=" + self.repo +
                        "\nExecStart=" + self.repo + "/runsvc.sh\n")
        unit.chmod(0o664)
        real_safe = auth._safe_directory
        fake_account = SimpleNamespace(pw_uid=0, pw_gid=0, pw_dir=str(self.base))

        def safe(path):
            if path == "/etc/systemd/system":
                return os.open(fake_systemd, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            return real_safe(path)

        def invoke(operation, action, version="-"):
            cli.main([operation, REPO, RUNNER, self.repo, action, version])

        with (mock.patch.dict(os.environ, {"SUDO_USER": "fixture"}),
              mock.patch("pwd.getpwnam", return_value=fake_account),
              mock.patch.object(cli, "_safe_directory", side_effect=safe),
              mock.patch.object(auth, "_safe_directory", side_effect=safe)):
            invoke("stage", "PRE_REGISTRATION")
            invoke("stage", "REGISTERED_PERMISSION_INCOMPLETE")
            invoke("attest", "-", "2.328.0")
            invoke("stage", "REGISTERED_UNIT_INCOMPLETE")
            with self.assertRaises(auth.AuthorityError):
                invoke("unit", "-")
            self.assertEqual(auth._cycle_record(
                auth.CREATE_STATES, REPO, RUNNER, self.repo,
                auth._current(REPO, RUNNER, self.repo)["instance"])["stage"],
                "REGISTERED_UNIT_INCOMPLETE")
            self.assertEqual(unit.stat().st_mode & 0o777, 0o664)
            unit.chmod(0o644)
            invoke("unit", "-")
            invoke("stage", "REGISTERED_START_INCOMPLETE")
            invoke("stage", "REGISTERED_HEALTH_UNKNOWN")
            invoke("stage", "CREATE_COMPLETE")
        self.assertEqual(len(list((self.base / "state" / "unit-attestations").glob("*.json"))), 1)

    def test_same_repo_new_lifecycle_uses_distinct_proofs_and_inode(self):
        self.complete_create()
        first = auth._current(REPO, RUNNER, self.repo)["instance"]
        old_attest = auth._cycle_record(auth.ATTESTATIONS, REPO, RUNNER, self.repo, first)
        self.assertEqual(old_attest["instance"], first)
        auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")
        auth.remove_stage(REPO, RUNNER, self.repo, "root", "CONFIRM_REMOTE_REMOVED")
        shutil.rmtree(self.runner)
        auth.remove_stage(REPO, RUNNER, self.repo, "root", "COMPLETE")
        self.runner.mkdir()
        (self.runner / ".runner").write_text(json.dumps({
            "agentName": RUNNER, "gitHubUrl": "https://github.com/example/repo"}))
        (self.runner / ".credentials").write_text("SECOND_CYCLE_SYNTHETIC")
        for name in (".runner", ".credentials"):
            (self.runner / name).chmod(0o600)
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        second = auth._current(REPO, RUNNER, self.repo)["instance"]
        self.assertNotEqual(first, second)
        self.assertIsNone(auth._cycle_record(auth.ATTESTATIONS, REPO, RUNNER, self.repo, second))
        with self.assertRaises(auth.AuthorityError):
            auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_UNIT_INCOMPLETE")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        auth.create_attestation(REPO, RUNNER, self.repo, "root", "2.328.0")
        new_attest = auth._cycle_record(auth.ATTESTATIONS, REPO, RUNNER, self.repo, second)
        self.assertNotEqual(old_attest["files"][".credentials"]["sha256"],
                            new_attest["files"][".credentials"]["sha256"])
        self.assertEqual(auth._cycle_record(auth.ATTESTATIONS, REPO, RUNNER, self.repo, first),
                         old_attest)
        with self.assertRaises(auth.AuthorityError):
            auth.remove_stage(REPO, RUNNER, self.repo, "root", "BEGIN")

    def test_attestation_wrong_repository_rejected(self):
        auth.create_stage(REPO, RUNNER, self.repo, "PRE_REGISTRATION")
        auth.create_stage(REPO, RUNNER, self.repo, "REGISTERED_PERMISSION_INCOMPLETE")
        with self.assertRaises(auth.AuthorityError):
            auth.create_attestation("foreign/repo", RUNNER, self.repo, "root", "2.328.0")


if __name__ == "__main__":
    unittest.main()
