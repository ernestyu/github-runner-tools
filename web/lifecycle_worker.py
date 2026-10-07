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

from grt_web_common import ProtocolError, load_key_value, recv_json_line, validate_repository


def read_request(fd: int) -> dict[str, Any]:
    sock = socket.socket(fileno=os.dup(fd))
    try:
        req = recv_json_line(sock)
    finally:
        sock.close()
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


def main() -> int:
    if os.environ.get("GRT_WEB_CONTEXT") != "1":
        return 70
    try:
        request_fd = int(os.environ["GRT_REQUEST_FD"])
        token_fd = int(os.environ["GRT_TOKEN_FD"])
        privileged_fd = int(os.environ["GRT_PRIVILEGED_FD"])
        config_path = os.environ["GRT_WEB_CONFIG"]
    except (KeyError, ValueError):
        return 70

    cfg = load_key_value(config_path)
    runner_user = cfg["RUNNER_USER"]
    pw = pwd.getpwnam(runner_user)
    if os.geteuid() != pw.pw_uid or os.getegid() != pw.pw_gid:
        return 71
    if os.getgroups():
        # Dispatcher must clear supplementary groups before lifecycle entry.
        return 72

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
    cli_dir = cfg.get("CLI_DIR", "/usr/local/lib/github-runner-tools/web/cli")
    status_script = os.path.join(cli_dir, "status-runners.sh")
    register_script = os.path.join(cli_dir, "register-runner.sh")
    remove_script = os.path.join(cli_dir, "remove-runner.sh")

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": cfg["RUNNER_HOME"],
        "USER": runner_user,
        "LOGNAME": runner_user,
        "GRT_WEB_CONTEXT": "1",
        "GRT_PRIVILEGED_FD": str(privileged_fd),
        "GRT_TOKEN_FD": str(token_fd),
        "GRT_WEB_LOCK_HELD": "1",
        "GRT_PTY_ADAPTER": cfg.get(
            "PTY_ADAPTER", "/usr/local/lib/github-runner-tools/web/pty_token_adapter.py"
        ),
    }
    archive_test = cfg.get("GRT_ARCHIVE_TEST_MODE")
    if archive_test:
        env["GRT_TEST_MODE"] = archive_test

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
