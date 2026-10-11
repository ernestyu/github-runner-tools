#!/usr/bin/env python3
"""Canonical root-owned 0644 GitHub Actions Runner Unit installer.

Shared by interactive CLI sudo authority and the bounded Executor's privileged
service_install. Never execute downloaded Runner svc.sh as root for install.

The official Linux Runner service installer copies bin/runsvc.sh to the runner
root before enabling the Unit. This module reproduces only that bounded,
verified bootstrap step plus canonical Unit publication.
"""
from __future__ import annotations

import hashlib
import os
import pwd
import stat
import subprocess

from grt_web_common import canonical_service_name, validate_repository

UNIT_DIR = "/etc/systemd/system"
MAX_WRAPPER_BYTES = 64 * 1024


class UnitInstallError(RuntimeError):
    """Trusted fixed-class install failure; detail never crosses into logs."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def unit_bytes(repo: str, runner_name: str, directory: str, user: str) -> bytes:
    validate_repository(repo)
    if not user or not user.replace("_", "").replace("-", "").isalnum():
        raise UnitInstallError("authority_identity_rejected")
    if not runner_name or any(x in runner_name for x in ("/", "\n", "\r")):
        raise UnitInstallError("authority_identity_rejected")
    if not directory.startswith("/") or "\x00" in directory or "\n" in directory:
        raise UnitInstallError("authority_identity_rejected")
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


def _digest_fd(fd: int) -> str:
    h = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    total = 0
    while True:
        chunk = os.read(fd, 8192)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_WRAPPER_BYTES:
            raise UnitInstallError("runner_service_entrypoint_invalid")
        h.update(chunk)
    if total == 0:
        raise UnitInstallError("runner_service_entrypoint_invalid")
    os.lseek(fd, 0, os.SEEK_SET)
    return h.hexdigest()


def bootstrap_service_entrypoint(directory: str, user: str) -> str:
    """Publish verified bin/runsvc.sh as root-level runsvc.sh, no overwrite."""
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise UnitInstallError("authority_identity_rejected") from exc

    try:
        runnerfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY |
                           os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise UnitInstallError("authority_identity_rejected") from exc
    try:
        rst = os.fstat(runnerfd)
        if (rst.st_uid != account.pw_uid or rst.st_gid != account.pw_gid
                or rst.st_mode & 0o022):
            raise UnitInstallError("authority_identity_rejected")

        try:
            binfd = os.open("bin", os.O_RDONLY | os.O_DIRECTORY |
                            os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=runnerfd)
        except FileNotFoundError as exc:
            raise UnitInstallError("runner_service_entrypoint_missing") from exc
        except OSError as exc:
            raise UnitInstallError("runner_service_entrypoint_invalid") from exc

        try:
            try:
                sourcefd = os.open("runsvc.sh", os.O_RDONLY | os.O_NOFOLLOW |
                                   os.O_CLOEXEC, dir_fd=binfd)
            except FileNotFoundError as exc:
                raise UnitInstallError("runner_service_entrypoint_missing") from exc
            except OSError as exc:
                raise UnitInstallError("runner_service_entrypoint_invalid") from exc
            try:
                source_st = os.fstat(sourcefd)
                if (not stat.S_ISREG(source_st.st_mode)
                        or source_st.st_uid != account.pw_uid
                        or source_st.st_gid != account.pw_gid
                        or source_st.st_nlink != 1
                        or source_st.st_mode & 0o022
                        or not (source_st.st_mode & stat.S_IXUSR)):
                    raise UnitInstallError("runner_service_entrypoint_invalid")
                source_digest = _digest_fd(sourcefd)

                try:
                    os.stat("runsvc.sh", dir_fd=runnerfd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise UnitInstallError("runner_service_entrypoint_invalid")

                temp = f".grt-runsvc-new-{os.getpid()}"
                try:
                    destfd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o700,
                                     dir_fd=runnerfd)
                except OSError as exc:
                    raise UnitInstallError("unit_stage_create_failed") from exc
                try:
                    try:
                        os.lseek(sourcefd, 0, os.SEEK_SET)
                        while True:
                            chunk = os.read(sourcefd, 8192)
                            if not chunk:
                                break
                            view = memoryview(chunk)
                            while view:
                                n = os.write(destfd, view)
                                if n <= 0:
                                    raise OSError("short write")
                                view = view[n:]
                        os.fsync(destfd)
                        os.fchown(destfd, account.pw_uid, account.pw_gid)
                        os.fchmod(destfd, 0o755)
                    except OSError as exc:
                        raise UnitInstallError("unit_stage_create_failed") from exc
                finally:
                    os.close(destfd)

                try:
                    checkfd = os.open(temp, os.O_RDONLY | os.O_NOFOLLOW |
                                      os.O_CLOEXEC, dir_fd=runnerfd)
                    try:
                        dst_st = os.fstat(checkfd)
                        if (not stat.S_ISREG(dst_st.st_mode)
                                or dst_st.st_uid != account.pw_uid
                                or dst_st.st_gid != account.pw_gid
                                or dst_st.st_nlink != 1
                                or stat.S_IMODE(dst_st.st_mode) != 0o755
                                or _digest_fd(checkfd) != source_digest):
                            raise UnitInstallError("runner_service_entrypoint_invalid")
                    finally:
                        os.close(checkfd)

                    try:
                        os.link(temp, "runsvc.sh", src_dir_fd=runnerfd,
                                dst_dir_fd=runnerfd, follow_symlinks=False)
                    except OSError as exc:
                        raise UnitInstallError("unit_publish_failed") from exc
                    os.unlink(temp, dir_fd=runnerfd)
                    os.fsync(runnerfd)
                except Exception:
                    try:
                        os.unlink(temp, dir_fd=runnerfd)
                    except FileNotFoundError:
                        pass
                    raise
            finally:
                os.close(sourcefd)
        finally:
            os.close(binfd)
    finally:
        os.close(runnerfd)
    return source_digest


def install_canonical_unit(repo: str, runner_name: str, directory: str,
                           user: str, *, timeout: float = 20.0) -> str:
    """Bootstrap official wrapper, publish canonical Unit, never overwrite."""
    if os.geteuid() != 0:
        raise UnitInstallError("authority_install_failed")
    service = canonical_service_name(repo, runner_name)
    payload = unit_bytes(repo, runner_name, directory, user)

    # Official Runner archives ship bin/runsvc.sh; official svc.sh install
    # copies it to the runner root. Do the bounded equivalent first.
    bootstrap_service_entrypoint(directory, user)

    try:
        unit_dir = os.open(UNIT_DIR, os.O_RDONLY | os.O_DIRECTORY |
                           os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise UnitInstallError("authority_install_failed") from exc
    try:
        path = os.path.join(UNIT_DIR, service)
        if os.path.lexists(path):
            raise UnitInstallError("unit_target_exists")
        tmp = "." + service + ".grt-new-" + str(os.getpid())
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                         os.O_CLOEXEC | os.O_NOFOLLOW, 0o644, dir_fd=unit_dir)
        except OSError as exc:
            raise UnitInstallError("unit_stage_create_failed") from exc
        try:
            try:
                os.fchown(fd, 0, 0)
                os.fchmod(fd, 0o644)
                if os.write(fd, payload) != len(payload):
                    raise OSError("short write")
                os.fsync(fd)
            except OSError as exc:
                raise UnitInstallError("unit_stage_create_failed") from exc
        finally:
            os.close(fd)
        try:
            if os.path.lexists(path):
                raise UnitInstallError("unit_target_exists")
            try:
                os.link(tmp, service, src_dir_fd=unit_dir, dst_dir_fd=unit_dir,
                        follow_symlinks=False)
            except OSError as exc:
                raise UnitInstallError("unit_publish_failed") from exc
        finally:
            try:
                os.unlink(tmp, dir_fd=unit_dir)
            except FileNotFoundError:
                pass
    finally:
        os.close(unit_dir)

    try:
        subprocess.run(["/usr/bin/systemctl", "daemon-reload"],
                       timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnitInstallError("unit_daemon_reload_failed") from exc
    try:
        subprocess.run(["/usr/bin/systemctl", "enable", service],
                       timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnitInstallError("unit_enable_failed") from exc
    return service
