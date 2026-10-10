#!/usr/bin/env python3
"""One bounded V2 lifecycle transaction: existing CLI remains authority.

The root Executor owns the existing mutation lock, spawns the fixed
UID-dropping CLI launcher, and services the existing root-only lifecycle
operations through a private socketpair. The root firewall delegates to the
existing lifecycle state library and canonical Unit validator. No generic
sudo, shell, worker process, or second lifecycle state machine.
"""
from __future__ import annotations

import fcntl
import json
import os
import pwd
import socket
import stat
import time
from pathlib import Path

from dispatcher import Runtime
from v2_cli_spawn import spawn_cli, terminate_group
from v2_executor_contract import Request
from v2_privileged import execute_privileged, PrivilegedProtocolError

CONFIG = "/etc/github-runner-tools/web.conf"
MAX_PRIV_MESSAGE = 16384
MAX_CLI_OUTPUT = 131072


class MutationError(RuntimeError):
    pass


def lock_transaction(runtime: Runtime):
    """Reuse V1/CLI common root-owned lock file, never create a second lock."""
    runtime.ensure_lock_infrastructure()
    path = runtime.lock_file
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or (
            meta.st_gid != runtime.runner_gr.gr_gid
        ) or stat.S_IMODE(meta.st_mode) != 0o660:
            raise MutationError("invalid shared lifecycle lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MutationError("operation_in_progress") from exc
        return fd
    except BaseException:
        os.close(fd)
        raise


def execute_mutation(request: Request, secret: bytes | None,
                     *, config_path: str = CONFIG) -> dict:
    """Single transaction, with durable stage decisions owned by existing CLI."""
    if request.op not in ("create", "remove") or not secret:
        return {"ok": False, "error": "invalid_mutation"}
    if config_path != CONFIG or os.geteuid() != 0:
        return {"ok": False, "error": "executor_configuration_rejected"}
    try:
        runtime = Runtime(config_path)
        lock_fd = lock_transaction(runtime)
    except MutationError as exc:
        if str(exc) == "operation_in_progress":
            return {"ok": False, "error": "operation_in_progress"}
        return {"ok": False, "error": "state_unavailable"}
    except (OSError, KeyError, RuntimeError, ValueError):
        return {"ok": False, "error": "state_unavailable"}

    try:
        # Independently compare the exact Web Remove tuple with root-owned
        # cycle, registration attestation, .runner ID and validated Unit.
        # Existing CLI then performs the durable BEGIN checkpoint before
        # contacting GitHub, preventing ordinary retry after UNKNOWN.
        if request.op == "remove":
            from v2_remove_guard import check_remove_target, RemoveGuardError
            try:
                check_remove_target(runtime, request)
            except RemoveGuardError:
                return {"ok": False, "error": "remove_identity_rejected"}
        deadline = time.monotonic() + min(runtime.timeout, 900)
        root_socket, child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Keep FD 4's source >=5 as required by the pinned spawn map.
            child_fd = child_socket.detach()
            if child_fd < 5:
                import fcntl as _fcntl
                replacement = _fcntl.fcntl(child_fd, _fcntl.F_DUPFD_CLOEXEC, 5)
                os.close(child_fd)
                child_fd = replacement
            try:
                pid, output_fd = spawn_cli(request, secret, child_fd)
            except (OSError, ValueError):
                return {"ok": False, "error": "launcher_unavailable"}
            finally:
                os.close(child_fd)
            from v2_event_loop import pump_transaction
            status, timed_out = pump_transaction(
                runtime, request, pid, root_socket, output_fd, deadline
            )
            from v2_failure import diagnostic
            if timed_out:
                # Never synthesize CREATE_COMPLETE after a crashed CLI.
                return diagnostic(runtime, request, "outcome_unknown")
            if status is None or not os.WIFEXITED(status):
                return diagnostic(runtime, request, "outcome_unknown")
            if os.WEXITSTATUS(status) != 0:
                return diagnostic(runtime, request, "lifecycle_failed",
                                  exit_code=os.WEXITSTATUS(status))
            return {"ok": True}
        finally:
            try:
                root_socket.close()
            except OSError:
                pass
            try:
                child_socket.close()
            except OSError:
                pass
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
