#!/usr/bin/env python3
"""UID-dropped lifecycle worker for Web Management V1."""
from __future__ import annotations

import ctypes
import json
import os
import pwd
import re
import socket
import subprocess
import sys
from typing import Any

from grt_web_common import ProtocolError, validate_repository


_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_CAPABILITY_WORDS = 2
_PR_SET_KEEPCAPS = 8
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4

_IDENTITY_STAGES = {
    "worker_not_root",
    "capability_clear_failed",
    "setgroups_failed",
    "setgid_failed",
    "setuid_failed",
    "identity_verification_failed",
}



REMOVE_STAGES = frozenset({
    "preflight_failed", "service_state_failed", "service_stop_failed",
    "service_uninstall_failed", "service_record_reconcile_failed", "config_remove_failed",
    "local_cleanup_failed", "unknown_failed",
})
REMOVE_MARKER = re.compile(
    rb"GRT_REMOVE_RESULT_V1 stage=(preflight_failed|service_state_failed|"
    rb"service_stop_failed|service_uninstall_failed|service_record_reconcile_failed|config_remove_failed|"
    rb"local_cleanup_failed|unknown_failed) exit=(unknown|0|[1-9][0-9]{0,2})\n"
)


def parse_remove_marker(raw: bytes, returncode: int) -> dict[str, Any]:
    unknown = {"ok": False, "error": "lifecycle_failed",
               "stage": "unknown_failed", "exit_code": None}
    if returncode <= 0 or len(raw) > 128:
        return unknown
    match = REMOVE_MARKER.fullmatch(raw)
    if match is None:
        return unknown
    stage = match.group(1).decode("ascii")
    value = match.group(2).decode("ascii")
    if stage not in REMOVE_STAGES or value == "0":
        return unknown
    exit_code = None if value == "unknown" else int(value)
    if exit_code is not None and exit_code > 255:
        return unknown
    return {"ok": False, "error": "lifecycle_failed",
            "stage": stage, "exit_code": exit_code}

class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


class IdentityDropError(RuntimeError):
    def __init__(self, stage: str, cause: BaseException | None = None):
        if stage not in _IDENTITY_STAGES:
            raise ValueError("invalid identity-drop stage")
        super().__init__(stage)
        self.stage = stage
        self.cause_type = type(cause).__name__ if cause is not None else None
        errno_value = getattr(cause, "errno", None)
        self.errno = errno_value if isinstance(errno_value, int) else None


def worker_diagnostic(stage: str, exc: IdentityDropError | None = None) -> None:
    """Emit fixed, bounded identity-drop metadata only."""
    if stage not in _IDENTITY_STAGES:
        stage = "identity_verification_failed"
    parts = [f"worker_diag stage={stage}"]
    if exc is not None and exc.cause_type:
        parts.append(f"exc={exc.cause_type}")
    if exc is not None and exc.errno is not None:
        parts.append(f"errno={exc.errno}")
    print(" ".join(parts), file=sys.stderr, flush=True)


def _capget_data() -> tuple[_CapHeader, Any]:
    libc = ctypes.CDLL(None, use_errno=True)
    header = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * _CAPABILITY_WORDS)()
    if libc.capget(ctypes.byref(header), ctypes.byref(data)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, "capget failed")
    return header, data


def _capset_data(header: _CapHeader, data: Any) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, "capset failed")


def read_capability_sets() -> dict[str, int]:
    status: dict[str, str] = {}
    with open("/proc/self/status", "r", encoding="utf-8") as handle:
        for line in handle:
            if ":" in line:
                key, value = line.split(":", 1)
                status[key] = value.strip()
    return {
        key: int(status.get(key, "0"), 16)
        for key in ("CapInh", "CapPrm", "CapEff", "CapAmb")
    }


def clear_capabilities_for_identity_drop() -> None:
    """Clear inheritable/ambient state without removing SETUID/SETGID yet."""
    libc = ctypes.CDLL(None, use_errno=True)

    # Ambient capabilities are never needed by the worker.
    if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, "PR_CAP_AMBIENT_CLEAR_ALL failed")

    # Preserve effective/permitted capabilities needed for setgroups/set*id,
    # while explicitly zeroing every inheritable capability word.
    header, data = _capget_data()
    for entry in data:
        entry.inheritable = 0
    _capset_data(header, data)

    # Do not preserve permitted capabilities across the later root->actions
    # UID transition, even if the parent process changed this prctl setting.
    if libc.prctl(_PR_SET_KEEPCAPS, 0, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, "PR_SET_KEEPCAPS failed")

    caps = read_capability_sets()
    if caps["CapInh"] != 0 or caps["CapAmb"] != 0:
        raise PermissionError("capability pre-drop verification failed")


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


def privileged_peer_uid(fd: int) -> int:
    """Return SO_PEERCRED uid for the private dispatcher socket."""
    import struct

    sock = socket.socket(fileno=os.dup(fd))
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return int(uid)
    finally:
        sock.close()


def privileged_context_check(fd: int) -> None:
    # The JSON context_check response is not authority by itself: another
    # actions process can create its own socketpair and emulate that response.
    # The request-scoped private channel is authoritative only when its Unix
    # peer is the root dispatcher.
    if privileged_peer_uid(fd) != 0:
        raise RuntimeError("privileged dispatcher peer is not root")

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
        caps = read_capability_sets()
        if any(caps[key] != 0 for key in ("CapInh", "CapPrm", "CapEff", "CapAmb")):
            return False
    except (OSError, ValueError):
        return False
    return True


def drop_to_runner_identity(uid: int, gid: int) -> None:
    """Drop root completely before any runner lifecycle logic executes."""
    if os.geteuid() != 0:
        raise IdentityDropError("worker_not_root")

    # Security-sensitive order:
    # 1. Remove inheritable/ambient capability state while preserving the
    #    effective/permitted SETUID/SETGID authority needed for the next steps.
    # 2. Clear supplementary groups.
    # 3. Drop all real/effective/saved GIDs.
    # 4. Drop all real/effective/saved UIDs; with KEEPCAPS disabled Linux
    #    clears permitted/effective capabilities during this transition.
    # 5. Verify IDs, groups, and all required capability sets are zero.
    try:
        clear_capabilities_for_identity_drop()
    except (OSError, PermissionError) as exc:
        raise IdentityDropError("capability_clear_failed", exc) from exc

    try:
        os.setgroups([])
    except OSError as exc:
        raise IdentityDropError("setgroups_failed", exc) from exc

    try:
        if hasattr(os, "setresgid"):
            os.setresgid(gid, gid, gid)
        else:
            os.setgid(gid)
    except OSError as exc:
        raise IdentityDropError("setgid_failed", exc) from exc

    try:
        if hasattr(os, "setresuid"):
            os.setresuid(uid, uid, uid)
        else:
            os.setuid(uid)
    except OSError as exc:
        raise IdentityDropError("setuid_failed", exc) from exc

    if not verify_unprivileged_identity(uid, gid):
        raise IdentityDropError("identity_verification_failed")


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
    try:
        drop_to_runner_identity(pw.pw_uid, pw.pw_gid)
    except IdentityDropError as exc:
        worker_diagnostic(exc.stage, exc)
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

        result_read = result_write = -1
        if op == "remove":
            result_read, result_write = os.pipe()
            cmd[cmd.index("--lock-already-held"):cmd.index("--lock-already-held")] = [
                "--result-fd", str(result_write)
            ]
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                env=env,
                pass_fds=(token_fd, privileged_fd, result_write)
                if op == "remove" else (token_fd, privileged_fd),
                check=False,
            )
        finally:
            if result_write >= 0:
                os.close(result_write)
        marker = b""
        if result_read >= 0:
            try:
                # Do not block if a misbehaving descendant retained the pipe.
                import select
                if select.select([result_read], [], [], 0)[0]:
                    marker = os.read(result_read, 129)
            finally:
                os.close(result_read)
        if proc.returncode != 0:
            if op == "remove":
                print(json.dumps(parse_remove_marker(marker, proc.returncode)))
            else:
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
