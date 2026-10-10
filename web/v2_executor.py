#!/usr/bin/env python3
"""V2 Executor daemon: authenticated Unix socket and CLI Status.

Mutation operations intentionally fail closed until root-operation enum,
durable lifecycle guards and shared Create/Remove execution are wired. This
daemon MUST NOT be deployed as a functional replacement for V1 at this stage.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import socket
import stat
import sys
from pathlib import Path

from v2_executor_transport import ExecutorError, admit_peer, read_request_frame
from v2_status import StatusError, status_as_runner

SOCKET = "/run/github-runner-tools/executor.sock"
MAX_RESPONSE = 1048576


def execute(request, secret):
    if request.op == "create":
        from v2_mutation import execute_mutation
        return execute_mutation(request, secret)
    if request.op == "remove":
        from v2_mutation import execute_mutation
        return execute_mutation(request, secret)
    if secret is not None:
        return {"ok": False, "error": "invalid_request"}
    from v2_inventory import annotate_inventory
    return annotate_inventory(status_as_runner())


def service_one(connection, web_user="grt-web", execute_fn=execute):
    """Authenticate before reading data, and never echo untrusted input."""
    connection.settimeout(20)
    try:
        admit_peer(connection, web_user)
        req, secret = read_request_frame(connection)
        result = execute_fn(req, secret)
        if not isinstance(result, dict):
            raise ExecutorError("invalid result")
    except (ExecutorError, StatusError, OSError, ValueError):
        result = {"ok": False, "error": "rejected"}
    raw = json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(raw) > MAX_RESPONSE:
        raw = b'{"ok":false,"error":"response_too_large"}'
    connection.sendall(len(raw).to_bytes(4, "big") + raw)


def validate_socket_directory(path: Path) -> None:
    parent = path.parent
    if parent.is_symlink():
        raise ExecutorError("unsafe socket directory")
    st = parent.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_gid != 0 or stat.S_IMODE(st.st_mode) != 0o755:
        raise ExecutorError("unsafe socket directory")


def serve(path=SOCKET, web_user="grt-web"):
    if os.geteuid() != 0:
        raise ExecutorError("root Executor required")
    if path != SOCKET:
        raise ExecutorError("socket path must be fixed")
    pathobj = Path(path)
    validate_socket_directory(pathobj)
    web = pwd.getpwnam(web_user)
    if web_user != "grt-web":
        raise ExecutorError("unexpected Web identity")
    if pathobj.is_symlink() or pathobj.exists():
        raise ExecutorError("socket already exists; refuse to replace")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        try:
            os.chown(path, 0, web.pw_gid)
            os.chmod(path, 0o660)
            server.listen(8)
            while True:
                conn, _ = server.accept()
                with conn:
                    try:
                        service_one(conn, web_user)
                    except (BrokenPipeError, ConnectionResetError, socket.timeout):
                        pass
        finally:
            # Only remove the socket created by this process, not an
            # unexpected path that may have replaced it during shutdown.
            try:
                st = os.lstat(path)
                if stat.S_ISSOCK(st.st_mode) and st.st_uid == 0:
                    os.unlink(path)
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default=SOCKET)
    args = parser.parse_args()
    try:
        serve(args.socket)
    except (ExecutorError, OSError, KeyError):
        print("v2_executor: startup rejected", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
