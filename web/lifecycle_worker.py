#!/usr/bin/env python3
"""UID-dropped lifecycle worker for Web Management V1."""
from __future__ import annotations

import ctypes
import json
import os
import pwd
import socket
import subprocess
import sys
from typing import Any

from grt_web_common import ProtocolError, validate_repository


def read_request(fd: int) -> dict[str, Any]:
    data = bytearray()
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            raise ProtocolError("request pipe closed")
        data.extend(chunk)
        if len(data) > 16384:
            raise ProtocolError("request too large")
        pos = data.find(b"\n")
        if pos >= 0:
            line = bytes(data[:pos])
            break
    try:
        req = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid request JSON") from exc
    if not isinstance(req, dict):
        raise ProtocolError("request must be object")
    return req


def privileged_context_check(fd: int) -> None:
    sock = socket.socket(fileno=os.dup(fd))
    file = sock.makefile("rwb", buffering=0)
    try:
        file.write(b'{"op":"context_check"}\n')
        raw = file.readline(4096)
        reply = json.loads(raw.decode("utf-8"))
        if reply != {"ok": True, "context": "dispatcher"}:
            raise RuntimeError("invalid dispatcher context")
    finally:
        file.close()
        sock.close()


def disable_ptrace_dumpability() -> None:
    # Same-UID runner jobs must not be able to inspect this request-scoped
    # worker and duplicate its private privileged control FD.
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_DUMPABLE = 4
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE) failed")


def verify_unprivileged_identity(uid: int, gid: int) -> bool:
    if os.geteuid() != uid or os.getegid() != gid:
        return False
    if hasattr(os, "getresuid") and os.getresuid() != (uid, uid, uid):
        return False
    if hasattr(os, "getresgid") and os.getresgid() != (gid, gid, gid):
        return False
    if os.getgroups():
        return False
    try:
        status = {}
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
        for key in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
            if int(status.get(key, "0"), 16) != 0:
                return False
    except (OSError, ValueError):
        return False
    return True


def main() -> int:
    if os.environ.get("GRT_WEB_CONTEXT") != "1":
        return 70
    try:
        request_fd = int(os.environ["GRT_REQUEST_FD"])
        token_fd = int(os.environ["GRT_TOKEN_FD"])
        privileged_fd = int(os.environ["GRT_PRIVILEGED_FD"])
        runner_user = os.environ["GRT_RUNNER_USER"]
        runner_home = os.environ["GRT_RUNNER_HOME"]
        cli_dir = os.environ["GRT_CLI_DIR"]
        pty_adapter = os.environ["GRT_PTY_ADAPTER"]
    except (KeyError, ValueError):
        return 70

    pw = pwd.getpwnam(runner_user)
    if not verify_unprivileged_identity(pw.pw_uid, pw.pw_gid):
        return 71

    try:
        disable_ptrace_dumpability()
    except OSError:
        return 72

    try:
        privileged_context_check(privileged_fd)
        request = read_request(request_fd)
    except Exception:
        return 73

    op = request.get("op")
    status_script = os.path.join(cli_dir, "status-runners.sh")
    register_script = os.path.join(cli_dir, "register-runner.sh")
    remove_script = os.path.join(cli_dir, "remove-runner.sh")

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": runner_home,
        "USER": runner_user,
        "LOGNAME": runner_user,
        "GRT_WEB_CONTEXT": "1",
        "GRT_PRIVILEGED_FD": str(privileged_fd),
        "GRT_TOKEN_FD": str(token_fd),
        "GRT_WEB_LOCK_HELD": "1",
        "GRT_PTY_ADAPTER": pty_adapter,
    }

    try:
        if op == "list":
            cmd = [status_script, "--json"]
        else:
            repository = validate_repository(str(request["repository"]))
            if op == "create":
                cmd = [
                    register_script,
                    "--web-worker",
                    "--token-fd",
                    str(token_fd),
                    "--privileged-fd",
                    str(privileged_fd),
                    "--lock-already-held",
                    repository,
                ]
            elif op == "remove":
                cmd = [
                    remove_script,
                    "--web-worker",
                    "--token-fd",
                    str(token_fd),
                    "--privileged-fd",
                    str(privileged_fd),
                    "--lock-already-held",
                    repository,
                ]
            elif op == "recover_local":
                cmd = [
                    remove_script,
                    "--web-worker",
                    "--privileged-fd",
                    str(privileged_fd),
                    "--lock-already-held",
                    "--recover-local",
                    repository,
                ]
            else:
                print(json.dumps({"ok": False, "error": "unknown_operation"}))
                return 0

        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            env=env,
            pass_fds=(token_fd, privileged_fd),
            check=False,
        )
        if proc.returncode != 0:
            print(json.dumps({"ok": False, "error": "lifecycle_failed"}))
            return 0
        if op == "list":
            try:
                runners = json.loads(proc.stdout)
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_status_json"}))
                return 0
            print(json.dumps({"ok": True, "runners": runners}, separators=(",", ":")))
        else:
            print(json.dumps({"ok": True}, separators=(",", ":")))
        return 0
    except (KeyError, ValueError, OSError):
        print(json.dumps({"ok": False, "error": "worker_error"}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
