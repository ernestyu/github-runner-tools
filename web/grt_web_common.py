#!/usr/bin/env python3
"""Shared Web Management V1 helpers."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
from typing import Any

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_REPOSITORY_LEN = 200
MAX_TOKEN_LEN = 1024
MAX_REQUEST_BYTES = 8192
MAX_PROTOCOL_BYTES = 16384

DEFAULT_DISPATCH_SOCKET = "/run/github-runner-tools/web-dispatch.sock"
DEFAULT_LOCK_FILE = "/run/lock/github-runner-tools/mutation.lock"
DEFAULT_CONFIG = "/etc/github-runner-tools/web.conf"
DEFAULT_AUTH_CONFIG = "/etc/github-runner-tools/web-auth.conf"


class ProtocolError(ValueError):
    pass


def load_key_value(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"invalid config line in {path}")
            key, value = line.split("=", 1)
            if not re.fullmatch(r"[A-Z0-9_]+", key):
                raise ValueError(f"invalid config key in {path}: {key}")
            values[key] = value
    return values


def validate_repository(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("repository must be text")
    if len(value) > MAX_REPOSITORY_LEN or not REPOSITORY_RE.fullmatch(value):
        raise ValueError("repository must be in OWNER/REPO form")
    return value


def sanitize_component(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("-")
    if not value:
        raise ValueError("empty sanitized component")
    return value


def make_local_id(owner: str, repo: str, max_len: int = 64, hash_len: int = 8) -> str:
    safe_owner = sanitize_component(owner)
    safe_repo = sanitize_component(repo)
    direct = f"{safe_owner}--{safe_repo}"
    if len(direct) <= max_len:
        return direct
    digest = hashlib.sha256(f"{owner.lower()}/{repo.lower()}".encode()).hexdigest()[:hash_len]
    available = max_len - 2 - 2 - hash_len
    if available < 2:
        raise ValueError("local id limit too small")
    base_each = available // 2
    owner_len = len(safe_owner)
    repo_len = len(safe_repo)
    if owner_len < base_each:
        repo_len = min(available - owner_len, len(safe_repo))
        owner_len = available - repo_len
    elif repo_len < base_each:
        owner_len = min(available - repo_len, len(safe_owner))
        repo_len = available - owner_len
    else:
        owner_len = base_each
        repo_len = available - owner_len
    if owner_len < 1 or repo_len < 1:
        raise ValueError("cannot derive local id")
    return f"{safe_owner[:owner_len]}--{safe_repo[:repo_len]}--{digest}"


def normalized_service_scope(repository: str) -> str:
    validate_repository(repository)
    owner, repo = repository.split("/", 1)
    return re.sub(r"[^0-9A-Za-z._-]", "-", f"{owner}-{repo}")


def default_runner_name(repository: str) -> str:
    owner, repo = validate_repository(repository).split("/", 1)
    return f"local-ci-{make_local_id(owner, repo)}"


def canonical_service_name(repository: str, runner_name: str) -> str:
    if not runner_name or any(c in runner_name for c in "\r\n/"):
        raise ValueError("invalid runner name")
    name = f"actions.runner.{normalized_service_scope(repository)}.{runner_name}.service"
    if len(name) > 150:
        raise ValueError("service name would require truncation")
    return name


def encode_json_line(value: Any) -> bytes:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_PROTOCOL_BYTES:
        raise ProtocolError("protocol message too large")
    return raw + b"\n"


def recv_json_line(sock: socket.socket, limit: int = MAX_PROTOCOL_BYTES) -> Any:
    data = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProtocolError("connection closed")
        data.extend(chunk)
        if len(data) > limit:
            raise ProtocolError("protocol message too large")
        pos = data.find(b"\n")
        if pos >= 0:
            line = bytes(data[:pos])
            break
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON") from exc


def password_hash(password: str, *, n: int = 2**15, r: int = 8, p: int = 1) -> str:
    if not password:
        raise ValueError("password cannot be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    parts = [
        "scrypt",
        str(n),
        str(r),
        str(p),
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    ]
    return "$".join(parts)

def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_s, digest_s = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_s.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_s.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def secure_random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
