#!/usr/bin/env python3
"""Canonical root-owned 0644 GitHub Actions Runner Unit installer.

Shared by interactive CLI sudo authority and the bounded Executor's privileged
service_install. Never execute downloaded Runner svc.sh as root for install.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from grt_web_common import canonical_service_name, validate_repository

UNIT_DIR = "/etc/systemd/system"


class UnitInstallError(RuntimeError):
    pass


def unit_bytes(repo: str, runner_name: str, directory: str, user: str) -> bytes:
    validate_repository(repo)
    if not user or not user.replace("_", "").replace("-", "").isalnum():
        raise UnitInstallError("invalid runner user")
    if not runner_name or any(x in runner_name for x in ("/", "\n", "\r")):
        raise UnitInstallError("invalid runner name")
    if not directory.startswith("/") or "\x00" in directory or "\n" in directory:
        raise UnitInstallError("invalid runner directory")
    runsvc = directory + "/runsvc.sh"
    text = (
        "[Unit]\n"
        f"Description=GitHub Actions Runner ({repo})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"WorkingDirectory={directory}\n"
        f"ExecStart={runsvc}\n"
        "KillMode=process\n"
        "KillSignal=SIGTERM\n"
        "TimeoutStopSec=5min\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return text.encode("utf-8")


def install_canonical_unit(repo: str, runner_name: str, directory: str,
                           user: str, *, timeout: float = 20.0) -> str:
    """Write a new root-owned 0644 unit; never modify an existing unit."""
    if os.geteuid() != 0:
        raise UnitInstallError("root authority required")
    service = canonical_service_name(repo, runner_name)
    payload = unit_bytes(repo, runner_name, directory, user)
    runsvc = directory + "/runsvc.sh"
    st = os.stat(runsvc, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or not (st.st_mode & stat.S_IXUSR):
        raise UnitInstallError("runner entrypoint unavailable")
    unit_dir = os.open(UNIT_DIR, os.O_RDONLY | os.O_DIRECTORY |
                      os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        path = os.path.join(UNIT_DIR, service)
        if os.path.lexists(path):
            raise UnitInstallError("Unit already exists")
        tmp = "." + service + ".grt-new-" + str(os.getpid())
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                     os.O_CLOEXEC | os.O_NOFOLLOW, 0o644, dir_fd=unit_dir)
        try:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o644)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            # O_EXCL guards the generated staging file. Existing target
            # check is necessary but cannot replace a final directory audit.
            if os.path.lexists(path):
                raise UnitInstallError("Unit appeared during installation")
            # Hard-link publishing fails with EEXIST instead of overwriting a
            # concurrently created systemd Unit. Unlink the private staging
            # name afterward, leaving a single-link root-owned target.
            os.link(tmp, service, src_dir_fd=unit_dir, dst_dir_fd=unit_dir,
                    follow_symlinks=False)
        finally:
            try:
                os.unlink(tmp, dir_fd=unit_dir)
            except FileNotFoundError:
                pass
    finally:
        os.close(unit_dir)
    # Do not delete this Unit on a later failure: keep an auditable incomplete
    # installation to be reconciled manually (no false Create success).
    subprocess.run(["/usr/bin/systemctl", "daemon-reload"],
                   timeout=timeout, check=True)
    subprocess.run(["/usr/bin/systemctl", "enable", service],
                   timeout=timeout, check=True)
    return service
