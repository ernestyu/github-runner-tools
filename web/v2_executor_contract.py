#!/usr/bin/env python3
"""V2 Executor admission contract. No lifecycle mutation or privileged execution.

Frozen design: d5b935ec99400e8cb3e0496af4172ec4840eb97c.
This module is intentionally pure, enabling a bounded executor to validate
the complete external request before opening a token FD or launching the CLI.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from grt_web_common import make_local_id, validate_repository

MAX_REQUEST_BYTES = 4096
MAX_TOKEN_BYTES = 1024
GITHUB_RUNNER_ID_MAX = (1 << 63) - 1
_INSTANCE = re.compile(r"^[0-9a-f]{32}$")
_RUNNER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

# Explicitly no nonce, confirmation proof or user-provided executable path.
FIELDS = {
    "create": frozenset(("op", "repository")),
    "remove": frozenset(("op", "repository", "runner_name", "runner_id", "instance")),
    "status": frozenset(("op",)),
}
CLI_ARGV = {
    "create": ("/usr/local/lib/github-runner-tools/web/cli/register-runner.sh",
               "--executor-mode", "--token-fd", "3", "--privileged-fd", "4",
               "--lock-already-held"),
    "remove": ("/usr/local/lib/github-runner-tools/web/cli/remove-runner.sh",
               "--executor-mode", "--token-fd", "3", "--privileged-fd", "4",
               "--lock-already-held"),
    "status": ("/usr/local/lib/github-runner-tools/web/cli/status-runners.sh", "--json"),
}


class AdmissionError(ValueError):
    """A safe rejection: do not echo caller-controlled values in errors."""


@dataclass(frozen=True)
class Request:
    op: Literal["create", "remove", "status"]
    repository: str | None = None
    runner_name: str | None = None
    runner_id: int | None = None
    instance: str | None = None

    @property
    def argv(self) -> tuple[str, ...]:
        if self.op == "status":
            return CLI_ARGV["status"]
        assert self.repository is not None
        return (*CLI_ARGV[self.op], self.repository)


def decode_request(raw: bytes) -> Request:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_REQUEST_BYTES:
        raise AdmissionError("invalid request size")
    try:
        payload = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, ValueError) as exc:
        raise AdmissionError("invalid request encoding") from exc
    if not isinstance(payload, dict):
        raise AdmissionError("request must be an object")
    op = payload.get("op")
    if not isinstance(op, str) or op not in FIELDS:
        raise AdmissionError("unsupported operation")
    if payload.keys() != FIELDS[op]:
        raise AdmissionError("invalid request fields")
    if op == "status":
        return Request(op="status")
    try:
        repository = validate_repository(payload["repository"])
        owner, name = repository.split("/", 1)
        make_local_id(owner, name)
    except (TypeError, ValueError) as exc:
        raise AdmissionError("invalid repository") from exc
    if op == "create":
        return Request(op="create", repository=repository)
    name = payload["runner_name"]
    runner_id = payload["runner_id"]
    instance = payload["instance"]
    if not isinstance(name, str) or not _RUNNER_NAME.fullmatch(name):
        raise AdmissionError("invalid runner name")
    if type(runner_id) is not int or not (0 < runner_id <= GITHUB_RUNNER_ID_MAX):
        raise AdmissionError("invalid runner ID")
    if not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
        raise AdmissionError("invalid instance ID")
    return Request(op="remove", repository=repository,
                   runner_name=name, runner_id=runner_id, instance=instance)


def validate_token_payload(raw: bytes) -> bytes:
    """Validate transient transport bytes; caller must not log or persist them."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_TOKEN_BYTES:
        raise AdmissionError("invalid token length")
    if any(c in raw for c in (b"\x00", b"\r", b"\n")):
        raise AdmissionError("invalid token bytes")
    try:
        raw.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise AdmissionError("invalid token encoding") from exc
    return raw
