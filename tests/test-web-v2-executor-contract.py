#!/usr/bin/env python3
"""Unit tests for the frozen V2 external request contract."""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web"))
from v2_executor_contract import AdmissionError, decode_request, validate_token_payload


def encode(obj):
    return json.dumps(obj).encode("utf-8")


class ContractTests(unittest.TestCase):
    def test_create_argv_is_fixed(self):
        request = decode_request(encode({"op": "create", "repository": "ernestyu/github-runner-tools"}))
        self.assertEqual(request.argv, (
            "/usr/local/lib/github-runner-tools/cli/register-runner.sh",
            "--executor-mode", "--token-fd", "3", "--privileged-fd", "4",
            "--lock-already-held", "ernestyu/github-runner-tools"))

    def test_status_is_read_only_and_tokenless(self):
        self.assertEqual(decode_request(b'{"op":"status"}').argv,
                         ("/usr/local/lib/github-runner-tools/cli/status-runners.sh", "--json"))
        with self.assertRaises(AdmissionError):
            decode_request(b'{"op":"status","token":"dummy"}')

    def test_remove_identity(self):
        request = decode_request(encode({
            "op": "remove", "repository": "ernestyu/github-runner-tools",
            "runner_name": "local-ci-ernestyu--github-runner-tools",
            "runner_id": 14, "instance": "ab" * 16}))
        self.assertEqual(request.runner_id, 14)
        self.assertEqual(request.instance, "ab" * 16)
        self.assertNotIn("token", request.__dict__)

    def test_remove_rejects_nonce_and_extra_fields(self):
        basic = {"op": "remove", "repository": "ernestyu/github-runner-tools",
                 "runner_name": "test", "runner_id": 1, "instance": "fa" * 16}
        for extra in ("nonce", "csrf", "command", "runner_dir", "force", "token"):
            with self.subTest(extra=extra), self.assertRaises(AdmissionError):
                decode_request(encode({**basic, extra: "x"}))

    def test_remove_rejects_ambiguous_identity(self):
        base = {"op": "remove", "repository": "ernestyu/github-runner-tools",
                "runner_name": "test", "runner_id": 1, "instance": "ab" * 16}
        mutations = [
            {"runner_id": True}, {"runner_id": 0}, {"runner_id": -1},
            {"runner_id": "1"}, {"instance": "../state"},
            {"instance": "AA" * 16}, {"runner_name": "../test"},
            {"runner_name": ""}, {"repository": "../../etc/passwd"},
        ]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(AdmissionError):
                decode_request(encode({**base, **changes}))

    def test_refuse_other_operations_and_missing_keys(self):
        for obj in ({"op": "shell", "command": "id"},
                    {"op": "recover_local", "repository": "o/r"},
                    {"op": "create"}, {"op": "create", "repository": "o/r", "token": "x"},
                    ["status"], {}):
            with self.subTest(obj=obj), self.assertRaises(AdmissionError):
                decode_request(encode(obj))

    def test_size_bound(self):
        with self.assertRaises(AdmissionError):
            decode_request(b"a" * 4097)

    def test_token_validation_avoids_echo(self):
        self.assertEqual(validate_token_payload(b"temporary-token"), b"temporary-token")
        for value in (b"", b"secret\n", b"secret\r", b"secret\x00", b"\xff", b"x" * 1025):
            with self.subTest(value=value[:7]), self.assertRaises(AdmissionError) as caught:
                validate_token_payload(value)
            self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
