#!/usr/bin/env python3
"""Root dispatcher for Web Management V1.

The dispatcher authenticates the grt-web Unix peer, owns the shared mutation
lock, launches one fixed worker after UID/GID drop, and services only the
narrow root-controlled systemd operations defined by the frozen SPEC.
"""
from __future__ import annotations

import argparse
import fcntl
import grp
import json
import os
import pwd
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from grt_web_common import (
    DEFAULT_CONFIG,
    DEFAULT_DISPATCH_SOCKET,
    DEFAULT_LOCK_FILE,
    ProtocolError,
    canonical_service_name,
    encode_json_line,
    load_key_value,
    make_local_id,
    recv_json_line,
    sanitize_component,
    validate_repository,
    validate_temporary_token,
)

ALLOWED_PUBLIC = {
    "list": {"op"},
    "create": {"op", "repository", "token"},
    "remove": {"op", "repository", "token"},
    "recover_local": {"op", "repository"},
}
ALLOWED_PRIV = {
    "context_check",
    "service_install",
    "service_start",
    "service_stop",
    "service_restart",
    "service_state",
    "service_uninstall",
    "ensure_runner_dependencies",
}


class DispatchError(RuntimeError):
    pass


def stable_error(code: str) -> dict[str, Any]:
    return {"ok": False, "error": code}


class Runtime:
    def __init__(self, config_path: str):
        cfg = load_key_value(config_path)
        self.config_path = config_path
        self.runner_user = cfg["RUNNER_USER"]
        self.runner_home = os.path.realpath(cfg["RUNNER_HOME"])
        self.runner_group = cfg.get("RUNNER_GROUP") or self.runner_user
        self.web_user = cfg.get("WEB_USER", "grt-web")
        self.worker_path = cfg.get(
            "WORKER_PATH", "/usr/local/lib/github-runner-tools/web/lifecycle_worker.py"
        )
        self.socket_path = cfg.get("DISPATCH_SOCKET", DEFAULT_DISPATCH_SOCKET)
        self.lock_file = cfg.get("MUTATION_LOCK_FILE", DEFAULT_LOCK_FILE)
        self.timeout = int(cfg.get("MUTATION_TIMEOUT_SECONDS", "900"))
        self.runner_pw = pwd.getpwnam(self.runner_user)
        self.runner_gr = grp.getgrnam(self.runner_group)
        self.web_pw = pwd.getpwnam(self.web_user)

    def validate_fixed_worker(self) -> None:
        st = os.stat(self.worker_path, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise DispatchError("worker_not_root_controlled")

    def ensure_lock_infrastructure(self) -> None:
        lock_path = Path(self.lock_file)
        lock_dir = lock_path.parent

        if lock_dir.is_symlink():
            raise DispatchError("lock_directory_symlink")
        if lock_dir.exists():
            st = os.stat(lock_dir, follow_symlinks=False)
            if (
                not stat.S_ISDIR(st.st_mode)
                or st.st_uid != 0
                or st.st_gid != 0
                or stat.S_IMODE(st.st_mode) != 0o755
            ):
                raise DispatchError("lock_directory_invalid")
        else:
            lock_dir.mkdir(mode=0o755, parents=True, exist_ok=False)
            os.chown(lock_dir, 0, 0)
            os.chmod(lock_dir, 0o755)

        if lock_path.is_symlink():
            raise DispatchError("lock_file_symlink")
        if not lock_path.exists():
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o660)
            try:
                os.fchown(fd, 0, self.runner_gr.gr_gid)
                os.fchmod(fd, 0o660)
            finally:
                os.close(fd)

        st = os.stat(lock_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != 0
            or st.st_gid != self.runner_gr.gr_gid
            or stat.S_IMODE(st.st_mode) != 0o660
        ):
            raise DispatchError("lock_file_invalid")

    def validate_runner_dir(self, repository: str, path: str, *, allow_legacy: bool = True) -> str:
        repository = validate_repository(repository)
        owner, repo = repository.split("/", 1)
        real = os.path.realpath(path)
        new_dir = os.path.realpath(
            os.path.join(self.runner_home, "actions-runner-" + make_local_id(owner, repo))
        )
        allowed = {new_dir}
        if allow_legacy:
            legacy = os.path.realpath(
                os.path.join(self.runner_home, "actions-runner-" + sanitize_component(repo))
            )
            allowed.add(legacy)
        if real not in allowed or os.path.islink(path):
            raise DispatchError("invalid_runner_dir")
        return real

    def _systemctl_show(self, service: str) -> dict[str, str]:
        props = [
            "LoadState",
            "FragmentPath",
            "DropInPaths",
            "User",
            "WorkingDirectory",
            "ExecStart",
            "ExecStartPre",
            "ExecStartPost",
            "ExecStop",
            "ExecStopPost",
            "ExecReload",
        ]
        cmd = ["systemctl", "show", service]
        for prop in props:
            cmd += ["-p", prop]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            raise DispatchError("systemd_query_failed")
        out: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        for prop in props:
            out.setdefault(prop, "")
        return out

    @staticmethod
    def _exec_field_empty(value: str) -> bool:
        stripped = value.strip()
        return stripped in ("", "[]", "{}")

    def validate_unit(
        self,
        repository: str,
        runner_dir: str,
        runner_name: str,
        service: str,
        allow_absent: bool = False,
    ) -> tuple[str, dict[str, str]]:
        validate_repository(repository)
        runner_dir = self.validate_runner_dir(repository, runner_dir)
        expected = canonical_service_name(repository, runner_name)
        if service != expected:
            raise DispatchError("service_identity_mismatch")

        props = self._systemctl_show(service)
        if props["LoadState"] == "not-found":
            if allow_absent:
                unit_path = f"/etc/systemd/system/{service}"
                if os.path.lexists(unit_path):
                    raise DispatchError("service_absent_but_unit_present")
                return "absent", props
            raise DispatchError("service_absent")

        fragment = os.path.realpath(props["FragmentPath"])
        expected_fragment = f"/etc/systemd/system/{service}"
        if fragment != expected_fragment or os.path.islink(expected_fragment):
            raise DispatchError("unit_provenance_mismatch")

        st = os.stat(expected_fragment, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise DispatchError("unit_permissions_invalid")
        if props["DropInPaths"].strip():
            raise DispatchError("unit_dropin_forbidden")
        if props["User"] != self.runner_user:
            raise DispatchError("unit_user_mismatch")
        if os.path.realpath(props["WorkingDirectory"]) != runner_dir:
            raise DispatchError("unit_workdir_mismatch")

        expected_exec = os.path.join(runner_dir, "runsvc.sh")
        exec_start = props["ExecStart"]
        if expected_exec not in exec_start:
            raise DispatchError("unit_execstart_mismatch")
        # Canonical V1 units have exactly one start command and no other Exec*
        # hooks. Reject extra command separators/secondary executable entries.
        if exec_start.count("path=") not in (0, 1):
            raise DispatchError("unit_execstart_mismatch")
        for key in (
            "ExecStartPre",
            "ExecStartPost",
            "ExecStop",
            "ExecStopPost",
            "ExecReload",
        ):
            if not self._exec_field_empty(props[key]):
                raise DispatchError("unit_exec_surface_mismatch")
        return "present", props

    def install_service(self, request: dict[str, Any]) -> dict[str, Any]:
        repository = validate_repository(str(request["repository"]))
        runner_dir = self.validate_runner_dir(
            repository, str(request["runner_dir"]), allow_legacy=False
        )
        runner_name = str(request["runner_name"])
        service = canonical_service_name(repository, runner_name)
        runsvc = os.path.join(runner_dir, "runsvc.sh")
        st = os.stat(runsvc, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or not (st.st_mode & stat.S_IXUSR):
            raise DispatchError("runner_service_entrypoint_invalid")

        unit_path = f"/etc/systemd/system/{service}"
        if os.path.lexists(unit_path):
            raise DispatchError("unit_already_exists")

        unit = (
            "[Unit]\n"
            f"Description=GitHub Actions Runner ({repository})\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"User={self.runner_user}\n"
            f"WorkingDirectory={runner_dir}\n"
            f"ExecStart={runsvc}\n"
            "KillMode=process\n"
            "KillSignal=SIGTERM\n"
            "TimeoutStopSec=5min\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        tmp = f"{unit_path}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, unit.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chown(tmp, 0, 0)
        os.chmod(tmp, 0o644)
        os.replace(tmp, unit_path)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", service], check=True)
        self.validate_unit(repository, runner_dir, runner_name, service)
        return {"ok": True, "service": service}

    def privileged(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op not in ALLOWED_PRIV:
            return stable_error("unknown_privileged_operation")
        if op == "context_check":
            return {"ok": True, "context": "dispatcher"}

        if op == "ensure_runner_dependencies":
            # Web setup is responsible for host dependencies. The worker asks
            # only for a bounded verification point; there is no runner-owned
            # root installer fallback.
            return {"ok": True}

        allowed = {"op", "repository", "runner_dir", "runner_name", "service"}
        if set(request) - allowed:
            return stable_error("unknown_privileged_field")
        try:
            repository = validate_repository(str(request["repository"]))
            runner_dir = self.validate_runner_dir(str(request["runner_dir"]))
            runner_name = str(request["runner_name"])
            if op == "service_install":
                return self.install_service(request)

            service = str(request["service"])
            state, _ = self.validate_unit(
                repository, runner_dir, runner_name, service, allow_absent=True
            )
            if op == "service_state":
                if state == "absent":
                    return {"ok": True, "state": "absent"}
                active = subprocess.run(
                    ["systemctl", "is-active", service],
                    text=True,
                    capture_output=True,
                    check=False,
                ).stdout.strip()
                return {"ok": True, "state": "active" if active == "active" else "inactive"}

            if state == "absent":
                if op in ("service_stop", "service_uninstall"):
                    return {"ok": True, "state": "absent"}
                return stable_error("service_absent")

            if op == "service_start":
                subprocess.run(["systemctl", "start", service], check=True)
            elif op == "service_stop":
                subprocess.run(["systemctl", "stop", service], check=True)
            elif op == "service_restart":
                subprocess.run(["systemctl", "restart", service], check=True)
            elif op == "service_uninstall":
                subprocess.run(["systemctl", "disable", service], check=False)
                subprocess.run(["systemctl", "stop", service], check=False)
                # Revalidate after stop and before unlinking the unit.
                self.validate_unit(repository, runner_dir, runner_name, service)
                os.unlink(f"/etc/systemd/system/{service}")
                subprocess.run(["systemctl", "daemon-reload"], check=True)
                final_state, _ = self.validate_unit(
                    repository, runner_dir, runner_name, service, allow_absent=True
                )
                if final_state != "absent":
                    raise DispatchError("service_uninstall_not_final")
            else:
                return stable_error("unknown_privileged_operation")
            return {"ok": True}
        except (KeyError, ValueError, OSError, subprocess.CalledProcessError, DispatchError):
            return stable_error("privileged_validation_failed")


class DispatchHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: "DispatchServer" = self.server  # type: ignore[assignment]
        runtime = server.runtime
        try:
            pid, uid, gid = socket.getpeereid(self.request) if hasattr(socket, "getpeereid") else (None, None, None)
        except Exception:
            pid = uid = gid = None
        if uid is None:
            try:
                import struct
                raw = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                pid, uid, gid = struct.unpack("3i", raw)
            except Exception:
                self.request.sendall(encode_json_line(stable_error("peer_credentials_unavailable")))
                return
        if uid != runtime.web_pw.pw_uid:
            self.request.sendall(encode_json_line(stable_error("peer_not_authorized")))
            return

        try:
            request = recv_json_line(self.request)
            if not isinstance(request, dict):
                raise ProtocolError("request must be object")
            op = request.get("op")
            if op not in ALLOWED_PUBLIC:
                self.request.sendall(encode_json_line(stable_error("unknown_operation")))
                return
            if set(request) != ALLOWED_PUBLIC[op]:
                self.request.sendall(encode_json_line(stable_error("invalid_request_fields")))
                return
            if op != "list":
                validate_repository(str(request.get("repository", "")))
            token = request.get("token")
            if token is not None:
                try:
                    validate_temporary_token(token)
                except ValueError:
                    self.request.sendall(encode_json_line(stable_error("invalid_token")))
                    return
            result = server.execute(request)
            if isinstance(token, str):
                # Avoid returning descendant output; still scrub the stable
                # response defensively.
                encoded = json.dumps(result)
                if token in encoded:
                    result = stable_error("internal_error")
            self.request.sendall(encode_json_line(result))
        except (ProtocolError, ValueError):
            self.request.sendall(encode_json_line(stable_error("invalid_request")))
        except Exception:
            self.request.sendall(encode_json_line(stable_error("internal_error")))


class DispatchServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        super().__init__(runtime.socket_path, DispatchHandler)

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        runtime = self.runtime
        op = request["op"]
        lock_handle = None
        if op != "list":
            try:
                lock_handle = open(runtime.lock_file, "r+", encoding="utf-8")
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                if lock_handle:
                    lock_handle.close()
                return stable_error("operation_in_progress")

        deadline = time.monotonic() + runtime.timeout
        request_r, request_w = os.pipe()
        token_r, token_w = os.pipe()
        ctrl_parent, ctrl_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            worker_request = {k: v for k, v in request.items() if k != "token"}
            os.write(request_w, encode_json_line(worker_request))
            os.close(request_w)
            request_w = -1

            token = request.get("token", "")
            if token:
                os.write(token_w, token.encode("utf-8") + b"\n")
            os.close(token_w)
            token_w = -1

            runtime.validate_fixed_worker()
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "GRT_WEB_CONTEXT": "1",
                "GRT_REQUEST_FD": str(request_r),
                "GRT_TOKEN_FD": str(token_r),
                "GRT_PRIVILEGED_FD": str(ctrl_child.fileno()),
                "GRT_RUNNER_USER": runtime.runner_user,
                "GRT_RUNNER_HOME": runtime.runner_home,
                "GRT_CLI_DIR": "/usr/local/lib/github-runner-tools/web/cli",
                "GRT_PTY_ADAPTER": "/usr/local/lib/github-runner-tools/web/pty_token_adapter.py",
            }
            proc = subprocess.Popen(
                ["/usr/bin/python3", runtime.worker_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                pass_fds=(request_r, token_r, ctrl_child.fileno()),
                user=runtime.runner_pw.pw_uid,
                group=runtime.runner_pw.pw_gid,
                extra_groups=[],
                start_new_session=True,
            )
            os.close(request_r)
            request_r = -1
            os.close(token_r)
            token_r = -1
            ctrl_child.close()

            stop_event = threading.Event()

            def privileged_loop() -> None:
                file = ctrl_parent.makefile("rwb", buffering=0)
                try:
                    while not stop_event.is_set():
                        line = file.readline(16385)
                        if not line:
                            return
                        if len(line) > 16384:
                            return
                        try:
                            msg = json.loads(line.decode("utf-8"))
                            if not isinstance(msg, dict):
                                raise ValueError
                            response = runtime.privileged(msg, deadline=deadline)
                        except Exception:
                            response = stable_error("invalid_privileged_request")
                        file.write(encode_json_line(response))
                finally:
                    try:
                        file.close()
                    except Exception:
                        pass

            thread = threading.Thread(target=privileged_loop, daemon=True)
            thread.start()
            try:
                stdout, stderr = proc.communicate(timeout=runtime.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=5)
                return stable_error("operation_timed_out")
            finally:
                stop_event.set()
                try:
                    ctrl_parent.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                ctrl_parent.close()
                # Privileged operations are bounded by the same request
                # deadline; do not release the mutation lock while one is
                # still executing.
                thread.join()

            if proc.returncode != 0:
                return stable_error("lifecycle_failed")
            try:
                result = json.loads(stdout)
            except json.JSONDecodeError:
                return stable_error("invalid_worker_result")
            if not isinstance(result, dict):
                return stable_error("invalid_worker_result")
            return result
        finally:
            for fd in (request_r, request_w, token_r, token_w):
                if isinstance(fd, int) and fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            try:
                ctrl_parent.close()
            except Exception:
                pass
            try:
                ctrl_child.close()
            except Exception:
                pass
            if lock_handle is not None:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_handle.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("dispatcher must run as root", file=sys.stderr)
        return 1
    runtime = Runtime(args.config)
    runtime.validate_fixed_worker()
    runtime.ensure_lock_infrastructure()
    socket_path = Path(runtime.socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists() or socket_path.is_symlink():
        socket_path.unlink()
    server = DispatchServer(runtime)
    try:
        os.chown(runtime.socket_path, 0, runtime.web_pw.pw_gid)
        os.chmod(runtime.socket_path, 0o660)
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
