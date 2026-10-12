#!/usr/bin/env python3
"""V2 bounded Executor admission framing, before privileged CLI execution.

This is protocol enforcement, not a runnable service: Create/Remove remain
disabled until authenticated root operations and UID-drop are implemented.
"""
from __future__ import annotations
import os
import pwd
import socket
import struct
from v2_executor_contract import AdmissionError, Request, decode_request, validate_token_payload

MAX_FRAME = 4096
MAX_SECRET = 1024

class ExecutorError(RuntimeError):
    pass

def unix_peer_uid(connection: socket.socket) -> int:
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    except (OSError, ValueError, struct.error) as exc:
        raise ExecutorError("peer verification failed") from exc

def admit_peer(connection: socket.socket, web_user: str = "grt-web") -> None:
    if os.geteuid() != 0:
        raise ExecutorError("root executor required")
    try:
        expected = pwd.getpwnam(web_user).pw_uid
    except KeyError as exc:
        raise ExecutorError("Web identity unavailable") from exc
    if unix_peer_uid(connection) != expected:
        raise ExecutorError("unauthorized peer")

def read_request_frame(connection: socket.socket) -> tuple[Request, bytes | None]:
    """uint32 JSON length, exact JSON, uint16 secret length, exact secret.

    One authenticated request per Unix socket connection; no implicit retry.
    Status requires empty secret. Create/Remove require exactly one secret.
    """
    def exactly(count: int) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            part = connection.recv(count - len(buf))
            if not part:
                raise ExecutorError("incomplete request")
            buf.extend(part)
        return bytes(buf)
    length = int.from_bytes(exactly(4), "big")
    if not 0 < length <= MAX_FRAME:
        raise ExecutorError("invalid request size")
    try:
        request = decode_request(exactly(length))
    except AdmissionError as exc:
        raise ExecutorError("invalid operation request") from exc
    secret_size = int.from_bytes(exactly(2), "big")
    if request.op == "status":
        if secret_size != 0:
            raise ExecutorError("status must not contain token")
        return request, None
    if not 1 <= secret_size <= MAX_SECRET:
        raise ExecutorError("invalid token length")
    try:
        return request, validate_token_payload(exactly(secret_size))
    except AdmissionError as exc:
        raise ExecutorError("invalid temporary token") from exc

def encode_request_frame(request: Request, token: bytes | None) -> bytes:
    """Web-side encoder: fixed request fields; secret outside JSON."""
    import json
    if request.op == "status":
        if token is not None:
            raise ExecutorError("status must not contain token")
    else:
        if token is None:
            raise ExecutorError("lifecycle operation requires token")
        validate_token_payload(token)
    payload = {"op": request.op}
    if request.repository is not None:
        payload["repository"] = request.repository
    if request.op == "remove":
        payload.update(runner_name=request.runner_name, runner_id=request.runner_id,
                       instance=request.instance)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_FRAME:
        raise ExecutorError("request too large")
    secret = token or b""
    return len(raw).to_bytes(4, "big") + raw + len(secret).to_bytes(2, "big") + secret
