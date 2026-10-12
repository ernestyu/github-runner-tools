#!/usr/bin/env python3
"""No-follow, mount-aware, directory-FD-relative normal Web Remove cleanup.

This is NOT a generic cleanup command. Caller must have confirmed official
unregister, root verified Unit absence, and persisted terminal cleanup state.
The caller passes the inode captured in that root-controlled terminal record.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys


class CleanupRejected(RuntimeError):
    pass


def _anchored(path: str) -> int:
    if not path.startswith("/") or os.path.normpath(path) != path or os.path.realpath(path) != path:
        raise CleanupRejected("uncanonical path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.split("/")[1:]:
            if part:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                              os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = nxt
        return fd
    except Exception:
        os.close(fd)
        raise


def _mount_id(fd: int) -> int:
    # Linux mount IDs distinguish same-device bind mounts. Not just st_dev.
    with open(f"/proc/self/fdinfo/{fd}", "r", encoding="ascii") as handle:
        for line in handle:
            if line.startswith("mnt_id:"):
                return int(line.split(":", 1)[1].strip())
    raise CleanupRejected("cannot establish mount identity")


def _safe_entry(dirfd: int, name: str, mount_id: int, dev: int) -> tuple[os.stat_result, int]:
    if not name or name in (".", "..") or "/" in name:
        raise CleanupRejected("invalid child name")
    pathfd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
    st = os.fstat(pathfd)
    if st.st_dev != dev or _mount_id(pathfd) != mount_id:
        os.close(pathfd)
        raise CleanupRejected("crossed mount boundary")
    if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
        os.close(pathfd)
        raise CleanupRejected("hardlink file forbidden")
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)):
        os.close(pathfd)
        raise CleanupRejected("special file forbidden")
    if st.st_uid != os.getuid() and not stat.S_ISLNK(st.st_mode):
        os.close(pathfd)
        raise CleanupRejected("foreign-owned entry")
    return st, pathfd


def _same(st: os.stat_result, new: os.stat_result) -> bool:
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode)) == (
        new.st_dev, new.st_ino, stat.S_IFMT(new.st_mode))


def _walk(dirfd: int, mount_id: int, dev: int, mutate: bool) -> None:
    names = os.listdir(dirfd)
    for name in names:
        st, pathfd = _safe_entry(dirfd, name, mount_id, dev)
        try:
            current = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            if not _same(st, current):
                raise CleanupRejected("entry replaced")
            if stat.S_ISDIR(st.st_mode):
                sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY |
                              os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
                try:
                    if not _same(st, os.fstat(sub)) or _mount_id(sub) != mount_id:
                        raise CleanupRejected("directory replaced or mounted")
                    _walk(sub, mount_id, dev, mutate)
                finally:
                    os.close(sub)
                if mutate:
                    if not _same(st, os.stat(name, dir_fd=dirfd, follow_symlinks=False)):
                        raise CleanupRejected("child directory replaced")
                    os.rmdir(name, dir_fd=dirfd)
            elif mutate:
                if not _same(st, os.stat(name, dir_fd=dirfd, follow_symlinks=False)):
                    raise CleanupRejected("file replaced")
                os.unlink(name, dir_fd=dirfd)  # Symlink target is never followed.
        finally:
            os.close(pathfd)


def _safe_repo_path(repo: str, basename: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return False
    owner, project = repo.split("/", 1)
    def part(v):
        return re.sub("[^a-z0-9._-]+", "-", v.lower()).strip("-")
    local_id = part(owner) + "--" + part(project)
    if len(local_id) <= 64:
        return basename in ("actions-runner-" + local_id, "actions-runner-" + part(project))
    digest = hashlib.sha256(repo.lower().encode()).hexdigest()[:8]
    available = 52
    o, p = part(owner), part(project)
    ol, pl = len(o), len(p)
    if ol < available // 2:
        pl = min(available - ol, pl)
        ol = available - pl
    elif pl < available // 2:
        ol = min(available - pl, ol)
        pl = available - ol
    else:
        ol = available // 2
        pl = available - ol
    return basename == "actions-runner-" + o[:ol] + "--" + p[:pl] + "--" + digest


def cleanup(base: str, target: str, repo: str, device: int, inode: int) -> None:
    if target != os.path.join(base, os.path.basename(target)) or not _safe_repo_path(repo, os.path.basename(target)):
        raise CleanupRejected("runner path scope mismatch")
    basefd = _anchored(base)
    try:
        basename = os.path.basename(target)
        targetfd = os.open(basename, os.O_RDONLY | os.O_DIRECTORY |
                           os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=basefd)
        try:
            bstat = os.fstat(basefd)
            st = os.fstat(targetfd)
            if st.st_uid != os.getuid() or st.st_mode & 0o022:
                raise CleanupRejected("untrusted runner directory")
            if (st.st_dev, st.st_ino) != (device, inode):
                raise CleanupRejected("root-pinned runner directory identity changed")
            if st.st_dev != bstat.st_dev or _mount_id(targetfd) != _mount_id(basefd):
                raise CleanupRejected("runner directory is a separate mount")
            mnt = _mount_id(targetfd)
            _walk(targetfd, mnt, st.st_dev, False)
            if not _same(st, os.stat(basename, dir_fd=basefd, follow_symlinks=False)):
                raise CleanupRejected("runner directory path replaced")
            _walk(targetfd, mnt, st.st_dev, True)
        finally:
            os.close(targetfd)
        if not _same(st, os.stat(basename, dir_fd=basefd, follow_symlinks=False)):
            raise CleanupRejected("final runner path changed")
        os.rmdir(basename, dir_fd=basefd)
        try:
            os.stat(basename, dir_fd=basefd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise CleanupRejected("runner path unexpectedly reappeared")
    finally:
        os.close(basefd)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 6:
            raise CleanupRejected("invalid arguments")
        cleanup(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
    except Exception:
        # Failures are observed through the already fixed local_cleanup_failed marker.
        sys.exit(1)
