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
import select
import signal
import socket
import stat
import threading
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


def serve_private_ops(runtime: Runtime, transaction: Request,
                      connection: socket.socket, deadline: float,
                      stop: threading.Event) -> None:
    """Read only from the private root/CLI channel, never the public socket."""
    connection.settimeout(0.5)
    buffer = bytearray()
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            chunk = connection.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_PRIV_MESSAGE:
            break
        while b"\n" in buffer:
            raw, _, remaining = buffer.partition(b"\n")
            buffer = bytearray(remaining)
            try:
                req = json.loads(raw.decode("utf-8", "strict"))
                from v2_binding import authorize_private_request, TransactionBindingError
                admitted = authorize_private_request(runtime, transaction, req)
                response = execute_privileged(runtime, admitted, deadline=deadline)
            except (ValueError, UnicodeError, PrivilegedProtocolError, OSError):
                response = {"ok": False, "error": "privileged_request_rejected"}
            try:
                connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            except OSError:
                return


def _capture_output(fd: int, buffer: bytearray, stop: threading.Event) -> None:
    try:
        while True:
            fragment = os.read(fd, 8192)
            if not fragment:
                return
            if len(buffer) < MAX_CLI_OUTPUT:
                buffer.extend(fragment[:MAX_CLI_OUTPUT - len(buffer)])
    except OSError:
        return
    finally:
        os.close(fd)
        stop.set()


def _wait_child(pid: int, deadline: float) -> tuple[int | None, bool]:
    while time.monotonic() < deadline:
        try:
            current, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None, True
        if current == pid:
            return status, False
        time.sleep(0.05)
    terminate_group(pid)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    return None, True


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
            stop = threading.Event()
            thread = threading.Thread(
                target=serve_private_ops,
                args=(runtime, request, root_socket, deadline, stop),
                daemon=True,
            )
            thread.start()
            try:
                pid, output_fd = spawn_cli(request, secret, child_fd)
            except (OSError, ValueError):
                return {"ok": False, "error": "launcher_unavailable"}
            finally:
                os.close(child_fd)
            output = bytearray()
            captured = threading.Event()
            output_thread = threading.Thread(
                target=_capture_output,
                args=(output_fd, output, captured),
                daemon=True,
            )
            output_thread.start()
            status, timed_out = _wait_child(pid, deadline)
            stop.set()
            root_socket.close()
            thread.join(timeout=2)
            output_thread.join(timeout=2)
            if timed_out:
                # Never synthesize CREATE_COMPLETE after a crashed CLI.
                return {"ok": False, "error": "outcome_unknown"}
            if status is None or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
                return {"ok": False, "error": "lifecycle_failed"}
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
