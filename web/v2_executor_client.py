#!/usr/bin/env python3
"""V2 Web client for the authenticated Unix Executor socket.

Only Status is exposed during the restricted WIP period. Create/Remove are
not forwarded or represented as operational until lifecycle implementation
and exact Remove attestation are complete.
"""
from __future__ import annotations

import json
import socket

from v2_executor_contract import decode_request
from v2_executor_transport import encode_request_frame

EXECUTOR_SOCKET = "/run/github-runner-tools/executor.sock"
MAX_RESPONSE_BYTES = 1048576


class ExecutorClientError(RuntimeError):
    pass


def fetch_status(socket_path: str = EXECUTOR_SOCKET) -> dict:
    if socket_path != EXECUTOR_SOCKET:
        raise ExecutorClientError("unapproved Executor endpoint")
    request = decode_request(b'{"op":"status"}')
    frame = encode_request_frame(request, None)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(20)
    try:
        sock.connect(socket_path)
        sock.sendall(frame)
        def exactly(size):
            buf = bytearray()
            while len(buf) < size:
                chunk = sock.recv(size - len(buf))
                if not chunk:
                    raise ExecutorClientError("truncated Executor response")
                buf.extend(chunk)
            return bytes(buf)
        size = int.from_bytes(exactly(4), "big")
        if not 1 <= size <= MAX_RESPONSE_BYTES:
            raise ExecutorClientError("invalid Executor response length")
        result = json.loads(exactly(size).decode("utf-8", "strict"))
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ExecutorClientError("invalid Executor response")
        return result
    except (OSError, UnicodeError, ValueError) as exc:
        raise ExecutorClientError("Executor unavailable or invalid response") from exc
    finally:
        sock.close()


def web_list():
    result = fetch_status()
    if not result.get("ok"):
        return {"ok": False, "error": "status_unavailable"}
    runners = result.get("data")
    if not isinstance(runners, list):
        return {"ok": False, "error": "invalid_status"}
    # CLI Status can report 'can_remove' for terminal operation. That is NOT
    # sufficient evidence for V2 Web removal; only Executor attestation may
    # grant that action. WIP Web always displays a read-only inventory.
    rows = []
    for item in runners:
        if not isinstance(item, dict):
            return {"ok": False, "error": "invalid_status"}
        row = {key: item.get(key) for key in (
            "repository", "runner_name", "service_state", "management_state",
            "configured", "runner_dir",
        )}
        row["can_remove"] = False
        row["can_recover_local"] = False
        rows.append(row)
    return {"ok": True, "runners": rows}
