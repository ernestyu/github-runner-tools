#!/usr/bin/env python3
"""V2 root-operation firewall over the existing trusted lifecycle authority.

This module delegates to the existing V1 root validation and state library:
no second lifecycle state machine, no arbitrary shell command interface.
It is an internal CLI-to-Executor channel ONLY and must never be exposed
through the public Web request socket.
"""
from __future__ import annotations

import re
from typing import Any

# Preserve existing authoritative implementations of systemd validation,
# registration attestation, durable create/remove checkpoints and Unit modes.
from dispatcher import Runtime

_ALLOWED = {
    "create_state": frozenset({"op", "repository", "runner_dir", "runner_name", "stage"}),
    "registration_attestation_create": frozenset({"op", "repository", "runner_dir", "runner_name", "version"}),
    "registration_permission_normalize": frozenset({"op", "repository", "runner_dir", "runner_name"}),
    "remove_state": frozenset({"op", "repository", "runner_dir", "runner_name", "stage"}),
    "service_install": frozenset({"op", "repository", "runner_dir", "runner_name"}),
    "service_start": frozenset({"op", "repository", "runner_dir", "runner_name", "service"}),
    "service_stop": frozenset({"op", "repository", "runner_dir", "runner_name", "service"}),
    "service_state": frozenset({"op", "repository", "runner_dir", "runner_name", "service"}),
    "service_uninstall": frozenset({"op", "repository", "runner_dir", "runner_name", "service"}),
}
_CREATE_STAGES = frozenset({
    "BEGIN", "REGISTRATION_STARTED", "REGISTERED", "CREATE_COMPLETE",
})
_REMOVE_STAGES = frozenset({"BEGIN", "CONFIRM_REMOTE_REMOVED", "COMPLETE"})


class PrivilegedProtocolError(ValueError):
    pass


def admitted_operation(request: Any) -> dict[str, Any]:
    """Return a new exact-field request or reject before any root call."""
    if not isinstance(request, dict):
        raise PrivilegedProtocolError("invalid operation object")
    op = request.get("op")
    if not isinstance(op, str) or op not in _ALLOWED:
        raise PrivilegedProtocolError("operation not allowed")
    if request.keys() != _ALLOWED[op]:
        raise PrivilegedProtocolError("invalid operation fields")
    from grt_web_common import validate_repository
    try:
        repo = validate_repository(request["repository"])
    except (ValueError, TypeError) as exc:
        raise PrivilegedProtocolError("invalid repository") from exc
    owner, name = repo.split("/")
    if owner in (".", "..") or name in (".", ".."):
        raise PrivilegedProtocolError("invalid repository components")
    # These are *validation*, not trusted paths: Runtime.privileged validates
    # canonical runner directory, Unit ownership and attestation independently.
    path = request["runner_dir"]
    if not isinstance(path, str) or not path.startswith("/") or (
        len(path) > 512 or "//" in path or "/../" in path or "/./" in path
        or path.endswith(("/..", "/.")) or "\x00" in path
    ):
        raise PrivilegedProtocolError("invalid runner directory")
    runner = request["runner_name"]
    if not isinstance(runner, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", runner):
        raise PrivilegedProtocolError("invalid runner name")
    if op == "create_state" and request["stage"] not in _CREATE_STAGES:
        raise PrivilegedProtocolError("create stage not allowed")
    if op == "remove_state" and request["stage"] not in _REMOVE_STAGES:
        raise PrivilegedProtocolError("remove stage not allowed")
    if op == "registration_attestation_create":
        version = request["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise PrivilegedProtocolError("invalid runner version")
    if op.startswith("service_") and op != "service_install":
        service = request["service"]
        if not isinstance(service, str) or not re.fullmatch(r"actions\.runner\.[A-Za-z0-9_.@-]{1,180}\.service", service):
            raise PrivilegedProtocolError("invalid service identity")
    return dict(request)


def execute_privileged(runtime: Runtime, request: Any, *, deadline: float | None = None) -> dict[str, Any]:
    """Invoke existing root-only validation with narrowed V2 operation names."""
    admitted = admitted_operation(request)
    result = runtime.privileged(admitted, deadline=deadline)
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise PrivilegedProtocolError("invalid authority response")
    return result
