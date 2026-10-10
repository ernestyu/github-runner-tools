#!/usr/bin/env python3
"""Fixed V2 CLI UID-drop launcher.

Exec'd as a separate trusted root-owned process before dropping identity,
never called via Python subprocess preexec_fn in the threaded Executor.
The root Executor passes a fixed operation and already-open FDs 3 and 4;
this launcher never parses arbitrary commands or paths from its caller.

Not an authorization service: the Executor must first verify Web SO_PEERCRED,
attestation, mutation lock and the privileged socket's transaction binding.
"""
from __future__ import annotations

import ctypes
import os
import pwd
import sys
from v2_executor_contract import CLI_ARGV, decode_request, AdmissionError

PR_SET_NO_NEW_PRIVS = 38
PR_SET_KEEPCAPS = 8
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4


class LaunchError(RuntimeError):
    pass


def resolve_action(op: str, repo: str | None) -> tuple[str, ...]:
    if op not in CLI_ARGV:
        raise LaunchError("unsupported operation")
    if op == "status":
        if repo is not None:
            raise LaunchError("unexpected repository")
        return CLI_ARGV["status"]
    if not isinstance(repo, str):
        raise LaunchError("repository required")
    import json
    try:
        req = decode_request(json.dumps({"op": "create", "repository": repo}).encode())
    except AdmissionError as exc:
        raise LaunchError("invalid repository") from exc
    owner, name = req.repository.split("/", 1)
    if owner in (".", "..") or name in (".", ".."):
        raise LaunchError("unsafe repository component")
    return (*CLI_ARGV[op], req.repository)


def drop_identity(uid: int, gid: int) -> None:
    """Drop all supplementary groups and real/effective/saved root IDs."""
    if os.geteuid() != 0 or uid <= 0 or gid <= 0:
        raise LaunchError("invalid root launch identity")
    libc = ctypes.CDLL(None, use_errno=True)
    def prctl(cmd: int, arg: int) -> None:
        if libc.prctl(cmd, arg, 0, 0, 0) != 0:
            raise LaunchError("privilege drop failed")
    prctl(PR_SET_KEEPCAPS, 0)
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    prctl(PR_CAP_AMBIENT_CLEAR_ALL, 0)
    prctl(PR_SET_NO_NEW_PRIVS, 1)
    if (os.getresuid() != (uid, uid, uid) or
            os.getresgid() != (gid, gid, gid) or
            os.getgroups()):
        raise LaunchError("identity drop verification failed")
    # Linux capabilities are independently verified from /proc; do not assume
    # UID transition alone guarantees that an unusual container is safe.
    with open("/proc/self/status", encoding="ascii") as f:
        status = f.read()
    for key in ("CapInh:", "CapPrm:", "CapEff:", "CapAmb:"):
        values = [x.split(":", 1)[1].strip() for x in status.splitlines()
                  if x.startswith(key)]
        if values != ["0000000000000000"]:
            raise LaunchError("capabilities not cleared")


def launch(op: str, repo: str | None, runner_user: str = "actions") -> None:
    argv = resolve_action(op, repo)
    if runner_user != "actions":
        raise LaunchError("unsupported runner identity")
    if os.geteuid() != 0:
        raise LaunchError("root launcher required")
    info = pwd.getpwnam(runner_user)
    if op != "status":
        for fd in (3, 4):
            try:
                os.fstat(fd)
            except OSError as exc:
                raise LaunchError("missing privileged descriptor") from exc
        import socket
        import struct
        # CLI channel FD 4 must be connected to the root Executor, verified
        # by the kernel. A random pipe or a forged env var is not enough.
        ctrl = socket.socket(fileno=os.dup(4))
        try:
            raw = ctrl.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                  struct.calcsize("3i"))
            _pid, peer_uid, _gid = struct.unpack("3i", raw)
            if peer_uid != 0:
                raise LaunchError("privileged peer not root")
        finally:
            ctrl.close()
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": info.pw_dir, "USER": info.pw_name, "LOGNAME": info.pw_name,
    }
    if op != "status":
        env["GRT_EXECUTOR_CONTEXT"] = "1"
    drop_identity(info.pw_uid, info.pw_gid)
    os.chdir(info.pw_dir)
    os.execve(argv[0], list(argv), env)


def main() -> int:
    # Fixed operation, repo only: never accept command/path/flags from a caller.
    if len(sys.argv) not in (2, 3):
        return 2
    op = sys.argv[1]
    repo = sys.argv[2] if len(sys.argv) == 3 else None
    try:
        launch(op, repo)
    except (LaunchError, OSError, ValueError):
        print("v2_cli_launcher: restricted launch rejected", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
