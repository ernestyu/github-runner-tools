#!/usr/bin/env python3
"""Conservative local service record check / non-destructive quarantine.

Runs as the runner owner. Never unlinks a directory entry. Fail closed on
unexpected metadata, stale quarantine or an adversarial rename race.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

PREFIX = ".grt-service-reconcile-"
RENAME_NOREPLACE = 1


def rename_noreplace(fd: int, source: str, dest: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        raise RuntimeError("renameat2 unavailable")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(fd, os.fsencode(source), fd, os.fsencode(dest), RENAME_NOREPLACE):
        raise OSError(ctypes.get_errno(), "atomic rename failed")


def same(a: os.stat_result, b: os.stat_result) -> bool:
    return all(getattr(a, x) == getattr(b, x) for x in
               ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid",
                "st_nlink", "st_size", "st_mtime_ns"))


def same_directory(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino, a.st_uid, a.st_mode) == (b.st_dev, b.st_ino, b.st_uid, b.st_mode)


def record(fd: int, basename: str, uid: int, expected: bytes) -> os.stat_result:
    st = os.stat(basename, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1 or st.st_mode & 0o022:
        raise ValueError("unsafe record metadata")
    if st.st_size > 160:
        raise ValueError("oversize record")
    h = os.open(basename, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        current = os.fstat(h)
        data = os.read(h, 161)
        if not same(st, current) or data not in (expected, expected + b"\n"):
            raise ValueError("service record mismatch")
        return current
    finally:
        os.close(h)


def run(mode: str, base: str, target: str, repo: str, runner: str, unit: str) -> None:
    if mode not in ("check", "quarantine"):
        raise ValueError("invalid mode")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repo")
    if not runner or any(c in runner for c in "\r\n/"):
        raise ValueError("invalid runner")
    expected = f"actions.runner.{repo.replace('/', '-')}.{runner}.service"
    if unit != expected or len(unit) > 150:
        raise ValueError("service name mismatch")
    base = os.path.realpath(base)
    if os.path.realpath(target) != target or os.path.dirname(target) != base:
        raise ValueError("unexpected runner path")
    base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        name = os.path.basename(target)
        if not name.startswith("actions-runner-"):
            raise ValueError("unexpected runner directory")
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=base_fd)
        try:
            identity = os.fstat(fd)
            if identity.st_uid != os.getuid() or identity.st_mode & 0o022:
                raise ValueError("unsafe runner directory")
            if not same_directory(identity, os.stat(name, dir_fd=base_fd, follow_symlinks=False)):
                raise ValueError("runner directory replaced")
            entries = os.listdir(fd)
            if any(n.startswith(PREFIX) for n in entries):
                raise ValueError("stale quarantine")
            metadata_fd = os.open(".runner", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                if not stat.S_ISREG(os.fstat(metadata_fd).st_mode):
                    raise ValueError("metadata type")
                raw = os.read(metadata_fd, 16385)
                if len(raw) > 16384:
                    raise ValueError("oversize metadata")
                metadata = json.loads(raw)
            finally:
                os.close(metadata_fd)
            url = metadata.get("gitHubUrl", "")
            if not isinstance(url, str) or url.rstrip("/").lower() != ("https://github.com/" + repo).lower():
                raise ValueError("metadata repo mismatch")
            if metadata.get("agentName") != runner:
                raise ValueError("metadata name mismatch")
            st = record(fd, ".service", os.getuid(), unit.encode("ascii"))
            if mode == "check":
                return
            # A bounded atomic move; never unlink the moved object.
            if not same_directory(identity, os.stat(name, dir_fd=base_fd, follow_symlinks=False)):
                raise ValueError("directory changed")
            if not same(st, os.stat(".service", dir_fd=fd, follow_symlinks=False)):
                raise ValueError("record changed")
            quarantine = PREFIX + secrets.token_hex(16)
            rename_noreplace(fd, ".service", quarantine)
            try:
                moved = record(fd, quarantine, os.getuid(), unit.encode("ascii"))
                if not same(st, moved):
                    raise ValueError("moved inode differs")
                try:
                    os.stat(".service", dir_fd=fd, follow_symlinks=False)
                    raise ValueError("service record returned")
                except FileNotFoundError:
                    pass
                if not same_directory(identity, os.stat(name, dir_fd=base_fd, follow_symlinks=False)):
                    raise ValueError("runner directory changed")
            except Exception:
                try:
                    rename_noreplace(fd, quarantine, ".service")
                except Exception:
                    pass
                raise
        finally:
            os.close(fd)
    finally:
        os.close(base_fd)


if __name__ == "__main__":
    try:
        run(*sys.argv[1:])
    except Exception:
        # No paths, token data or raw exception contents in output.
        sys.exit(1)
