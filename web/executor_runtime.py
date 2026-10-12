#!/usr/bin/env python3
"""Root-only lifecycle/systemd authority used by the bounded Executor.

No public socket server or historical Web dispatch path is implemented here.
"""
from __future__ import annotations

import grp
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from v2_unit_installer import UnitInstallError
from typing import Any

from runner_lifecycle_authority import AuthorityError, create_attestation, create_stage, remove_stage, create_unit_attestation, normalize_new_registration, verify_service_install_provenance

from grt_web_common import (
    DEFAULT_CONFIG,
    DEFAULT_LOCK_FILE,
    canonical_service_name,
    load_key_value,
    make_local_id,
    validate_repository,
)

PRIV_FIELDS = {
    "context_check": {"op"},
    "create_state": {"op", "repository", "runner_dir", "runner_name", "stage"},
    "registration_attestation_create": {"op", "repository", "runner_dir", "runner_name", "version"},
    "registration_permission_normalize": {"op", "repository", "runner_dir", "runner_name"},
    "remove_state": {"op", "repository", "runner_dir", "runner_name", "stage"},
    "service_install": {"op", "repository", "runner_dir", "runner_name", "version"},
    "service_start": {"op", "repository", "runner_dir", "runner_name", "service"},
    "service_stop": {"op", "repository", "runner_dir", "runner_name", "service"},
    "service_restart": {"op", "repository", "runner_dir", "runner_name", "service"},
    "service_state": {"op", "repository", "runner_dir", "runner_name", "service"},
    "service_uninstall": {"op", "repository", "runner_dir", "runner_name", "service"},
}


class ExecutorRuntimeError(RuntimeError):
    pass


def stable_error(code: str) -> dict[str, Any]:
    return {"ok": False, "error": code}


def diagnostic(stage: str, exc: BaseException | None = None, **fields: Any) -> None:
    """Emit bounded metadata only; never exception text or request content."""
    parts = [f"executor_diag stage={stage}"]
    if exc is not None:
        parts.append(f"exc={type(exc).__name__}")
        errno_value = getattr(exc, "errno", None)
        if isinstance(errno_value, int):
            parts.append(f"errno={errno_value}")
    for key, value in fields.items():
        if isinstance(value, (int, bool)):
            parts.append(f"{key}={value}")
    print(" ".join(parts), file=sys.stderr, flush=True)


_CAP_SETGID_BIT = 1 << 6
_CAP_SETUID_BIT = 1 << 7
_REQUIRED_IDENTITY_CAPS = _CAP_SETGID_BIT | _CAP_SETUID_BIT


def executor_identity_capabilities_ready() -> bool:
    """Require SETUID/SETGID in both permitted and effective capability sets."""
    status: dict[str, str] = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
        permitted = int(status.get("CapPrm", "0"), 16)
        effective = int(status.get("CapEff", "0"), 16)
    except (OSError, ValueError):
        return False
    return (
        permitted & _REQUIRED_IDENTITY_CAPS == _REQUIRED_IDENTITY_CAPS
        and effective & _REQUIRED_IDENTITY_CAPS == _REQUIRED_IDENTITY_CAPS
    )


class Runtime:
    def __init__(self, config_path: str):
        cfg = load_key_value(config_path)
        self.config_path = config_path
        self.runner_user = cfg["RUNNER_USER"]
        self.runner_home = os.path.realpath(cfg["RUNNER_HOME"])
        self.runner_group = cfg.get("RUNNER_GROUP") or self.runner_user
        self.web_user = cfg.get("WEB_USER", "grt-web")
        self.lock_file = cfg.get("MUTATION_LOCK_FILE", DEFAULT_LOCK_FILE)
        self.timeout = int(cfg.get("MUTATION_TIMEOUT_SECONDS", "900"))
        self.runner_pw = pwd.getpwnam(self.runner_user)
        self.runner_gr = grp.getgrnam(self.runner_group)
        self.web_pw = pwd.getpwnam(self.web_user)
        if not executor_identity_capabilities_ready():
            diagnostic("executor_identity_caps_missing")
            raise RuntimeError("executor identity-transition capabilities unavailable")

    def ensure_lock_infrastructure(self) -> None:
        lock_path = Path(self.lock_file)
        lock_dir = lock_path.parent

        if lock_dir.is_symlink():
            raise ExecutorRuntimeError("lock_directory_symlink")
        if lock_dir.exists():
            st = os.stat(lock_dir, follow_symlinks=False)
            if (
                not stat.S_ISDIR(st.st_mode)
                or st.st_uid != 0
                or st.st_gid != 0
                or stat.S_IMODE(st.st_mode) != 0o755
            ):
                raise ExecutorRuntimeError("lock_directory_invalid")
        else:
            lock_dir.mkdir(mode=0o755, parents=True, exist_ok=False)
            os.chown(lock_dir, 0, 0)
            os.chmod(lock_dir, 0o755)

        if lock_path.is_symlink():
            raise ExecutorRuntimeError("lock_file_symlink")
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
            raise ExecutorRuntimeError("lock_file_invalid")

    def validate_runner_dir(self, repository: str, path: str) -> str:
        repository = validate_repository(repository)
        owner, repo = repository.split("/", 1)
        real = os.path.realpath(path)
        expected = os.path.realpath(
            os.path.join(self.runner_home, "actions-runner-" + make_local_id(owner, repo))
        )
        if real != expected or os.path.islink(path):
            raise ExecutorRuntimeError("invalid_runner_dir")
        return real

    @staticmethod
    def _remaining_timeout(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ExecutorRuntimeError("operation_deadline_exceeded")
        return remaining

    def _run(
        self,
        cmd: list[str],
        *,
        deadline: float | None = None,
        check: bool = False,
        text: bool = False,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            timeout=self._remaining_timeout(deadline),
            check=check,
            text=text,
            capture_output=capture_output,
        )

    def _systemctl_show(self, service: str, deadline: float | None = None) -> dict[str, str]:
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
        proc = self._run(cmd, deadline=deadline, text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            raise ExecutorRuntimeError("systemd_query_failed")
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

    def _validate_unit_file_schema(self, fragment: str, runner_dir: str) -> None:
        service: dict[str, list[str]] = {}
        section = ""
        with open(fragment, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line
                    continue
                if section != "[Service]" or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                service.setdefault(key.strip(), []).append(value.strip())

        expected_exec = os.path.join(runner_dir, "runsvc.sh")
        if service.get("User") != [self.runner_user]:
            raise ExecutorRuntimeError("unit_file_user_mismatch")
        if service.get("WorkingDirectory") != [runner_dir]:
            raise ExecutorRuntimeError("unit_file_workdir_mismatch")
        if service.get("ExecStart") != [expected_exec]:
            raise ExecutorRuntimeError("unit_file_execstart_mismatch")
        if "Group" in service and service["Group"] != [self.runner_group]:
            raise ExecutorRuntimeError("unit_file_group_mismatch")
        for key in (
            "ExecStartPre",
            "ExecStartPost",
            "ExecStop",
            "ExecStopPost",
            "ExecReload",
        ):
            if service.get(key):
                raise ExecutorRuntimeError("unit_file_exec_surface_mismatch")

    def validate_unit(
        self,
        repository: str,
        runner_dir: str,
        runner_name: str,
        service: str,
        allow_absent: bool = False,
        deadline: float | None = None,
    ) -> tuple[str, dict[str, str]]:
        validate_repository(repository)
        runner_dir = self.validate_runner_dir(repository, runner_dir)
        expected = canonical_service_name(repository, runner_name)
        if service != expected:
            raise ExecutorRuntimeError("service_identity_mismatch")

        props = self._systemctl_show(service, deadline=deadline)
        if props["LoadState"] == "not-found":
            if allow_absent:
                unit_path = f"/etc/systemd/system/{service}"
                if os.path.lexists(unit_path):
                    raise ExecutorRuntimeError("service_absent_but_unit_present")
                return "absent", props
            raise ExecutorRuntimeError("service_absent")

        fragment = os.path.realpath(props["FragmentPath"])
        expected_fragment = f"/etc/systemd/system/{service}"
        if fragment != expected_fragment or os.path.islink(expected_fragment):
            raise ExecutorRuntimeError("unit_provenance_mismatch")

        st = os.stat(expected_fragment, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ExecutorRuntimeError("unit_permissions_invalid")
        self._validate_unit_file_schema(expected_fragment, runner_dir)
        if props["DropInPaths"].strip():
            raise ExecutorRuntimeError("unit_dropin_forbidden")
        if props["User"] != self.runner_user:
            raise ExecutorRuntimeError("unit_user_mismatch")
        if os.path.realpath(props["WorkingDirectory"]) != runner_dir:
            raise ExecutorRuntimeError("unit_workdir_mismatch")

        expected_exec = os.path.join(runner_dir, "runsvc.sh")
        exec_start = props["ExecStart"]
        path_marker = f"path={expected_exec}"
        argv_marker = f"argv[]={expected_exec}"
        if path_marker not in exec_start or argv_marker not in exec_start:
            raise ExecutorRuntimeError("unit_execstart_mismatch")
        # Exactly one systemd start command is permitted.
        if exec_start.count("path=") != 1 or exec_start.count("argv[]=") != 1:
            raise ExecutorRuntimeError("unit_execstart_mismatch")
        for key in (
            "ExecStartPre",
            "ExecStartPost",
            "ExecStop",
            "ExecStopPost",
            "ExecReload",
        ):
            if not self._exec_field_empty(props[key]):
                raise ExecutorRuntimeError("unit_exec_surface_mismatch")
        return "present", props

    def install_service(self, request: dict[str, Any], deadline: float | None = None) -> dict[str, Any]:
        from v2_unit_installer import install_canonical_unit
        repository = validate_repository(str(request["repository"]))
        runner_dir = self.validate_runner_dir(
            repository, str(request["runner_dir"])
        )
        runner_name = str(request["runner_name"])
        version = str(request["version"])
        provenance = verify_service_install_provenance(
            repository, runner_name, runner_dir, self.runner_user, version)
        service = install_canonical_unit(
            repository, runner_name, runner_dir, self.runner_user,
            provenance["service_wrapper"],
            timeout=self._remaining_timeout(deadline) or 20,
        )
        self.validate_unit(repository, runner_dir, runner_name, service, deadline=deadline)
        create_unit_attestation(repository, runner_name, runner_dir, service,
                                f"/etc/systemd/system/{service}")
        return {"ok": True, "service": service}

    def privileged(self, request: dict[str, Any], deadline: float | None = None) -> dict[str, Any]:
        op = request.get("op")
        expected_fields = PRIV_FIELDS.get(op)
        if expected_fields is None:
            return stable_error("unknown_privileged_operation")
        if set(request) != expected_fields:
            return stable_error("invalid_privileged_fields")
        if op == "context_check":
            return {"ok": True, "context": "executor"}

        try:
            repository = validate_repository(str(request["repository"]))
            runner_dir = self.validate_runner_dir(repository, str(request["runner_dir"]))
            runner_name = str(request["runner_name"])
            if op == "create_state":
                strict_dir = self.validate_runner_dir(repository, runner_dir)
                create_stage(repository, runner_name, strict_dir, str(request["stage"]))
                return {"ok": True}
            if op == "registration_permission_normalize":
                strict_dir = self.validate_runner_dir(repository, runner_dir)
                normalize_new_registration(repository, runner_name, strict_dir, self.runner_user)
                return {"ok": True}
            if op == "registration_attestation_create":
                strict_dir = self.validate_runner_dir(repository, runner_dir)
                create_attestation(repository, runner_name, strict_dir,
                                   self.runner_user, str(request["version"]))
                return {"ok": True}
            if op == "remove_state":
                stage = str(request["stage"])
                if stage in ("CONFIRM_REMOTE_REMOVED", "COMPLETE"):
                    canonical = canonical_service_name(repository, runner_name)
                    current, _ = self.validate_unit(
                        repository, runner_dir, runner_name, canonical,
                        allow_absent=True, deadline=deadline)
                    if current != "absent":
                        raise ExecutorRuntimeError("cannot finalize cleanup while Unit exists")
                result = remove_stage(repository, runner_name, runner_dir,
                                      self.runner_user, stage)
                return {"ok": True, **result}
            if op == "service_install":
                return self.install_service(request, deadline=deadline)

            service = str(request["service"])
            state, _ = self.validate_unit(
                repository, runner_dir, runner_name, service, allow_absent=True, deadline=deadline
            )
            if op == "service_state":
                if state == "absent":
                    return {"ok": True, "state": "absent"}
                active = self._run(
                    ["systemctl", "is-active", service],
                    deadline=deadline,
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
                self._run(["systemctl", "start", service], deadline=deadline, check=True)
            elif op == "service_stop":
                self._run(["systemctl", "stop", service], deadline=deadline, check=True)
            elif op == "service_restart":
                self._run(["systemctl", "restart", service], deadline=deadline, check=True)
            elif op == "service_uninstall":
                self._run(["systemctl", "disable", service], deadline=deadline, check=False)
                self._run(["systemctl", "stop", service], deadline=deadline, check=False)
                # Revalidate after stop and before unlinking the unit.
                self.validate_unit(
                    repository, runner_dir, runner_name, service, deadline=deadline
                )
                os.unlink(f"/etc/systemd/system/{service}")
                self._run(["systemctl", "daemon-reload"], deadline=deadline, check=True)
                final_state, _ = self.validate_unit(
                    repository, runner_dir, runner_name, service, allow_absent=True, deadline=deadline
                )
                if final_state != "absent":
                    raise ExecutorRuntimeError("service_uninstall_not_final")
            else:
                return stable_error("unknown_privileged_operation")
            return {"ok": True}
        except (
            KeyError,
            ValueError,
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ExecutorRuntimeError,
            AuthorityError,
            UnitInstallError,
        ):
            return stable_error("privileged_validation_failed")

