#!/usr/bin/env python3
"""V2 root Executor -> fixed launcher handoff without fork-child Python hooks.

posix_spawn file actions atomically map temporary token pipe and private
Executor privilege socket to child FDs 3/4, with no parent process fd-table
changes. The launcher itself performs verified permanent UID/GID drop before
execing one of the pinned CLI scripts.

This is a process primitive; its caller still MUST own the shared mutation
lock, check durable lifecycle state and serve the root privilege socket.
"""
from __future__ import annotations

import os
import fcntl
import pwd
import signal
import socket
import time

from v2_executor_contract import Request
from v2_token_fd import make_token_pipe, close_quietly

LAUNCHER = "/usr/local/lib/github-runner-tools/web/v2_cli_launcher.py"
PYTHON = "/usr/bin/python3"


class SpawnError(RuntimeError):
    pass


def child_file_actions(token_fd: int, privilege_fd: int, stdout_fd: int) -> list[tuple]:
    """Use the kernel's posix_spawn FD map; no preexec_fn or shell.

    The two source FDs must be distinct and >=5. That avoids collision when
    replacing child 3 and 4 and prevents accidentally passing root's own
    privileged control socket to an unrelated child.
    """
    sources = (token_fd, privilege_fd, stdout_fd)
    if any(type(fd) is not int or fd < 5 for fd in sources) or len(set(sources)) != 3:
        raise SpawnError("invalid source descriptor layout")
    return [
        (os.POSIX_SPAWN_DUP2, token_fd, 3),
        (os.POSIX_SPAWN_DUP2, privilege_fd, 4),
        (os.POSIX_SPAWN_DUP2, stdout_fd, 1),
        (os.POSIX_SPAWN_DUP2, stdout_fd, 2),
        (os.POSIX_SPAWN_CLOSE, token_fd),
        (os.POSIX_SPAWN_CLOSE, privilege_fd),
        (os.POSIX_SPAWN_CLOSE, stdout_fd),
    ]


def spawn_cli(request: Request, token: bytes, privilege_fd: int) -> tuple[int, int]:
    """Spawn trusted launcher; return child PID and captured-output read FD.

    The caller must wait/kill the process group on deadline and close output
    FD. No subprocess is auto-retried, including UNKNOWN remote outcomes.
    """
    if request.op not in ("create", "remove") or not request.repository:
        raise SpawnError("invalid lifecycle operation")
    if os.geteuid() != 0:
        raise SpawnError("root Executor required")
    if type(privilege_fd) is not int or privilege_fd < 5:
        raise SpawnError("invalid privileged socket descriptor")
    # Verify socket type and privileged root peer before allowing any launch.
    import struct
    fd_copy = os.dup(privilege_fd)
    control = socket.socket(fileno=fd_copy)
    try:
        if control.family != socket.AF_UNIX or control.type != socket.SOCK_STREAM:
            raise SpawnError("invalid privileged socket")
        raw = control.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, peer_uid, _gid = struct.unpack("3i", raw)
        if peer_uid != 0:
            raise SpawnError("privileged peer must be root")
    finally:
        control.close()

    token_read = make_token_pipe(token)
    output_read, output_write = os.pipe2(os.O_CLOEXEC)
    try:
        # Rehome low-numbered pipe FDs, which can arise under a daemon whose
        # stdin/stdout/stderr are closed. Never reject valid inputs merely
        # because the kernel returned descriptor 3 or 4.
        if token_read < 5:
            relocated = fcntl.fcntl(token_read, fcntl.F_DUPFD_CLOEXEC, 5)
            close_quietly(token_read)
            token_read = relocated
        if output_write < 5 or output_write in (token_read, privilege_fd):
            relocated = fcntl.fcntl(output_write, fcntl.F_DUPFD_CLOEXEC, 5)
            close_quietly(output_write)
            output_write = relocated
        if token_read == privilege_fd:
            relocated = fcntl.fcntl(token_read, fcntl.F_DUPFD_CLOEXEC, 5)
            close_quietly(token_read)
            token_read = relocated
        if len({token_read, privilege_fd, output_write}) != 3:
            raise SpawnError("descriptor collision")
        actions = child_file_actions(token_read, privilege_fd, output_write)
        argv = [PYTHON, LAUNCHER, request.op, request.repository]
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
        # setpgroup=0 starts a separately killable group, without fork hooks.
        pid = os.posix_spawn(PYTHON, argv, env,
                             file_actions=actions, setpgroup=0)
        return pid, output_read
    except BaseException:
        close_quietly(output_read)
        raise
    finally:
        close_quietly(token_read)
        close_quietly(output_write)


def terminate_group(pid: int, *, grace: float = 2.0) -> None:
    """Cancel child and descendants on deadline, no automatic retry."""
    if type(pid) is not int or pid <= 0:
        raise SpawnError("invalid process ID")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        until = time.monotonic() + (grace if sig == signal.SIGTERM else 0.1)
        while time.monotonic() < until:
            try:
                current, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if current == pid:
                return
            time.sleep(0.02)
