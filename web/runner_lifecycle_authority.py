#!/usr/bin/env python3
"""Root-owned Create journal and registration attestation authority.

The dispatcher calls these functions under its existing mutation lock.
No runner-owned process can provide a content digest or ledger destination.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import time

ROOT = "/var/lib/github-runner-tools"
ATTESTATIONS = "runner-attestations"
CREATE_STATES = "create-state"
REMOVE_STATES = "remove-state"
UNIT_ATTESTATIONS = "unit-attestations"
ACTIVE_INSTANCES = "active-instances"

STAGES = (
    "PRE_REGISTRATION",
    "REGISTERED_PERMISSION_INCOMPLETE",
    "REGISTERED_UNIT_INCOMPLETE",
    "REGISTERED_START_INCOMPLETE",
    "REGISTERED_HEALTH_UNKNOWN",
    "CREATE_COMPLETE",
)
UNKNOWN = "REGISTRATION_OUTCOME_UNKNOWN"


class AuthorityError(RuntimeError):
    pass


def _assert_root() -> None:
    if os.geteuid() != 0:
        raise AuthorityError("root authority required")


def _safe_directory(path: str) -> int:
    """Reject symlinks in any directory component, including ancestors."""
    if not path.startswith("/") or os.path.normpath(path) != path:
        raise AuthorityError("invalid directory")
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


def _store(subdir: str) -> int:
    _assert_root()
    # Parent is a fixed root-controlled location. A pre-existing unexpected
    # object or mode is an error, not a reason to repair it implicitly.
    parent = os.path.dirname(ROOT)
    parentfd = _safe_directory(parent)
    try:
        try:
            os.mkdir(os.path.basename(ROOT), mode=0o700, dir_fd=parentfd)
        except FileExistsError:
            pass
        rootfd = os.open(os.path.basename(ROOT), os.O_RDONLY | os.O_DIRECTORY |
                         os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parentfd)
        try:
            st = os.fstat(rootfd)
            if st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o700:
                raise AuthorityError("unsafe state store")
            try:
                os.mkdir(subdir, mode=0o700, dir_fd=rootfd)
            except FileExistsError:
                pass
            fd = os.open(subdir, os.O_RDONLY | os.O_DIRECTORY |
                         os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=rootfd)
            st = os.fstat(fd)
            if st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o700:
                os.close(fd)
                raise AuthorityError("unsafe state subdirectory")
            return fd
        finally:
            os.close(rootfd)
    finally:
        os.close(parentfd)


def _key(repo: str, runner: str, runner_dir: str) -> str:
    if not runner or "\0" in runner:
        raise AuthorityError("invalid identity")
    return hashlib.sha256((repo + "\0" + runner + "\0" + runner_dir).encode()).hexdigest() + ".json"


def _instance_name(repo: str, runner: str, runner_dir: str, instance: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", instance):
        raise AuthorityError("invalid instance identifier")
    return _key(repo, runner, runner_dir)[:-5] + "." + instance + ".json"


def _current(repo: str, runner: str, runner_dir: str) -> dict | None:
    fd = _store(ACTIVE_INSTANCES)
    try:
        record = _read_record(fd, _key(repo, runner, runner_dir))
    finally:
        os.close(fd)
    if record is None:
        return None
    if (record.get("repository"), record.get("runner"), record.get("runner_dir")) != (
            repo, runner, runner_dir):
        raise AuthorityError("active index identity mismatch")
    if not re.fullmatch(r"[0-9a-f]{32}", str(record.get("instance", ""))):
        raise AuthorityError("invalid active instance")
    return record


def _cycle_record(store: str, repo: str, runner: str, runner_dir: str,
                  instance: str) -> dict | None:
    fd = _store(store)
    try:
        return _read_record(fd, _instance_name(repo, runner, runner_dir, instance))
    finally:
        os.close(fd)


def _active_update(repo: str, runner: str, runner_dir: str, item: dict) -> None:
    fd = _store(ACTIVE_INSTANCES)
    try:
        name = _key(repo, runner, runner_dir)
        _publish(fd, name, item, replace=_read_record(fd, name) is not None)
    finally:
        os.close(fd)


def _read_record(fd: int, filename: str) -> dict | None:
    try:
        h = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW |
                    os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(h)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1:
            raise AuthorityError("invalid ledger metadata")
        raw = os.read(h, 16385)
        if len(raw) > 16384:
            raise AuthorityError("ledger too large")
        item = json.loads(raw)
        if not isinstance(item, dict):
            raise AuthorityError("invalid ledger")
        return item
    finally:
        os.close(h)


def _publish(fd: int, filename: str, item: dict, *, replace: bool) -> None:
    blob = (json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(blob) > 16384:
        raise AuthorityError("ledger oversized")
    tmp = ".tmp-" + secrets.token_hex(16)
    f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
    try:
        os.write(f, blob)
        os.fsync(f)
    finally:
        os.close(f)
    try:
        if replace:
            os.replace(tmp, filename, src_dir_fd=fd, dst_dir_fd=fd)
        else:
            os.link(tmp, filename, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
    finally:
        os.unlink(tmp, dir_fd=fd) if _exists(fd, tmp) else None
    os.fsync(fd)


def _exists(fd: int, filename: str) -> bool:
    try:
        os.stat(filename, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def create_stage(repo: str, runner: str, runner_dir: str, stage: str) -> None:
    """Each successful reuse of a logical Runner slot gets a new immutable cycle ID."""
    _assert_root()
    if stage not in STAGES and stage != UNKNOWN:
        raise AuthorityError("invalid create stage")
    fd = _store(CREATE_STATES)
    try:
        active = _current(repo, runner, runner_dir)
        if stage == "PRE_REGISTRATION":
            if active is not None:
                previous = _cycle_record(CREATE_STATES, repo, runner, runner_dir,
                                         active["instance"])
                removed = _cycle_record(REMOVE_STATES, repo, runner, runner_dir,
                                        active["instance"])
                if (previous is None or previous.get("stage") != "CREATE_COMPLETE"
                        or removed is None or removed.get("stage") != "REMOVE_COMPLETE"):
                    raise AuthorityError("prior cycle not closed")
            st = os.stat(runner_dir, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o022:
                raise AuthorityError("unsafe fresh runner directory")
            if active is not None:
                removed = _cycle_record(REMOVE_STATES, repo, runner, runner_dir,
                                        active["instance"])
                if (st.st_dev, st.st_ino) == (
                        removed.get("directory_device"), removed.get("directory_inode")):
                    raise AuthorityError("prior runner inode reused")
            instance = secrets.token_hex(16)
            item = {"schema": 2, "instance": instance, "repository": repo,
                    "runner": runner, "runner_dir": runner_dir,
                    "directory_device": st.st_dev, "directory_inode": st.st_ino,
                    "stage": stage, "started_at": int(time.time())}
            name = _instance_name(repo, runner, runner_dir, instance)
            _publish(fd, name, item, replace=False)
            _active_update(repo, runner, runner_dir, {
                "schema": 2, "repository": repo, "runner": runner,
                "runner_dir": runner_dir, "instance": instance})
            return
        if active is None:
            raise AuthorityError("missing create cycle")
        instance = active["instance"]
        name = _instance_name(repo, runner, runner_dir, instance)
        prior = _read_record(fd, name)
        if prior is None or prior.get("instance") != instance:
            raise AuthorityError("missing matched cycle")
        previous = prior.get("stage")
        if previous == stage and stage != "PRE_REGISTRATION":
            return
        if previous not in STAGES or previous == "CREATE_COMPLETE":
            raise AuthorityError("create cannot be restarted")
        if stage == UNKNOWN:
            pass
        elif STAGES.index(stage) != STAGES.index(previous) + 1:
            raise AuthorityError("create stage skipped")
        if stage == "REGISTERED_UNIT_INCOMPLETE":
            proof = _cycle_record(ATTESTATIONS, repo, runner, runner_dir, instance)
            if (proof is None or proof.get("instance") != instance
                    or proof.get("directory_device") != prior["directory_device"]
                    or proof.get("directory_inode") != prior["directory_inode"]
                    or proof.get("capture_stage") != "registered-secure-before-service"):
                raise AuthorityError("missing instance-bound registration attestation")
        item = {**prior, "stage": stage, "updated_at": int(time.time())}
        _publish(fd, name, item, replace=True)
    finally:
        os.close(fd)


def _file_digest(dirfd: int, name: str, uid: int, gid: int) -> dict:
    st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_gid != gid
            or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600):
        raise AuthorityError("unsafe registration file")
    h = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                os.O_CLOEXEC, dir_fd=dirfd)
    try:
        actual = os.fstat(h)
        if (actual.st_dev, actual.st_ino, actual.st_ctime_ns) != (st.st_dev, st.st_ino, st.st_ctime_ns):
            raise AuthorityError("registration changed")
        digest = hashlib.sha256()
        total = 0
        while True:
            buf = os.read(h, 8192)
            if not buf:
                break
            total += len(buf)
            if total > 65536:
                raise AuthorityError("registration file too large")
            digest.update(buf)
        if os.fstat(h).st_mtime_ns != st.st_mtime_ns:
            raise AuthorityError("registration modified")
        return {"device": st.st_dev, "inode": st.st_ino,
                "sha256": digest.hexdigest(), "size": total, "mode": "0600"}
    finally:
        os.close(h)


def normalize_new_registration(repo: str, runner: str, runner_dir: str, user: str) -> None:
    """Constrain chmod to a just-registered, root-tracked Create cycle."""
    _assert_root()
    active = _current(repo, runner, runner_dir)
    if active is None:
        raise AuthorityError("no trusted Create instance")
    state = _cycle_record(CREATE_STATES, repo, runner, runner_dir, active["instance"])
    if state is None or state.get("stage") != "REGISTERED_PERMISSION_INCOMPLETE":
        raise AuthorityError("registration permissions not in Create stage")
    account = pwd.getpwnam(user)
    fd = _safe_directory(runner_dir)
    try:
        parent = os.fstat(fd)
        if ((parent.st_dev, parent.st_ino) !=
              (state["directory_device"], state["directory_inode"]) or
              parent.st_uid != account.pw_uid or parent.st_mode & 0o022):
            raise AuthorityError("Create directory ownership changed")
        meta_fd = os.open(".runner", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW |
                          os.O_CLOEXEC, dir_fd=fd)
        try:
            raw = os.read(meta_fd, 16385)
            if len(raw) > 16384:
                raise AuthorityError("oversize Runner identity")
            meta = json.loads(raw)
            if (meta.get("agentName") != runner or
                    meta.get("gitHubUrl", "").rstrip("/").lower() !=
                    ("https://github.com/" + repo).lower()):
                raise AuthorityError("new registration identity mismatch")
        finally:
            os.close(meta_fd)
        # Validate both entries before altering either; refuse links and ownership
        # anomalies even when chmod would mechanically be possible.
        st_map = {}
        handles = {}
        try:
            for name in (".runner", ".credentials"):
                h = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW |
                            os.O_CLOEXEC, dir_fd=fd)
                handles[name] = h
                st = os.fstat(h)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != account.pw_uid
                        or st.st_gid != account.pw_gid or st.st_nlink != 1
                        or stat.S_IMODE(st.st_mode) not in (0o600, 0o644, 0o664)):
                    raise AuthorityError("new registration metadata untrusted")
                path_st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (st.st_dev, st.st_ino, st.st_ctime_ns) != (
                        path_st.st_dev, path_st.st_ino, path_st.st_ctime_ns):
                    raise AuthorityError("registration path replaced")
                st_map[name] = st
            for name, h in handles.items():
                os.fchmod(h, 0o600)
                after = os.fstat(h)
                path = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (stat.S_IMODE(after.st_mode) != 0o600 or
                        (after.st_dev, after.st_ino) != (path.st_dev, path.st_ino)):
                    raise AuthorityError("registration normalization race")
        finally:
            for h in handles.values():
                os.close(h)
        path_st = os.stat(runner_dir, follow_symlinks=False)
        if (parent.st_dev, parent.st_ino) != (path_st.st_dev, path_st.st_ino):
            raise AuthorityError("Create target replaced")
    finally:
        os.close(fd)


def create_attestation(repo: str, runner: str, runner_dir: str, user: str, version: str) -> None:
    _assert_root()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise AuthorityError("invalid official runner version")
    # Provenance must arise from the active, known successful registration stage.
    statefd = _store(CREATE_STATES)
    try:
        active = _current(repo, runner, runner_dir)
        state = None if active is None else _read_record(
            statefd, _instance_name(repo, runner, runner_dir, active["instance"]))
        if (state is None or state.get("stage") != "REGISTERED_PERMISSION_INCOMPLETE"
                or state.get("instance") != active["instance"]):
            raise AuthorityError("attestation not in registration transaction")
    finally:
        os.close(statefd)
    account = pwd.getpwnam(user)
    dirfd = _safe_directory(runner_dir)
    try:
        parent = os.fstat(dirfd)
        if parent.st_uid != account.pw_uid or parent.st_mode & 0o022:
            raise AuthorityError("unsafe runner directory")
        agent = _file_digest(dirfd, ".runner", account.pw_uid, account.pw_gid)
        credentials = _file_digest(dirfd, ".credentials", account.pw_uid, account.pw_gid)
        meta = os.open(".runner", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
        try:
            config = json.loads(os.read(meta, 16385))
            if (config.get("agentName") != runner or
                config.get("gitHubUrl", "").rstrip("/").lower() !=
                    ("https://github.com/" + repo).lower()):
                raise AuthorityError("registration identity mismatch")
        finally:
            os.close(meta)
        if (os.fstat(dirfd).st_dev, os.fstat(dirfd).st_ino) != (parent.st_dev, parent.st_ino):
            raise AuthorityError("runner directory replaced")
        path_state = os.stat(runner_dir, follow_symlinks=False)
        if (path_state.st_dev, path_state.st_ino) != (parent.st_dev, parent.st_ino):
            raise AuthorityError("runner directory path replaced")
        item = {
            "schema": 2, "instance": active["instance"], "issuer": "root-dispatcher-create", "issued_at": int(time.time()),
            "repository": repo, "runner": runner, "runner_dir": runner_dir,
            "runner_uid": account.pw_uid, "runner_gid": account.pw_gid,
            "directory_device": parent.st_dev, "directory_inode": parent.st_ino,
            "files": {".runner": agent, ".credentials": credentials},
            "runner_version": version, "capture_stage": "registered-secure-before-service",
        }
    finally:
        os.close(dirfd)
    fd = _store(ATTESTATIONS)
    try:
        name = _instance_name(repo, runner, runner_dir, active["instance"])
        if _read_record(fd, name) is not None:
            raise AuthorityError("attestation already exists")
        _publish(fd, name, item, replace=False)
    finally:
        os.close(fd)


def remove_stage(repo: str, runner: str, runner_dir: str, user: str, stage: str) -> dict:
    """Persistent terminal cleanup evidence, managed only by root Dispatcher.

    BEGIN is before official remote unregister, CONFIRM is only after the
    Worker's official config.sh remove returns a definitive success.
    COMPLETE is after scoped directory deletion and absence verification.
    """
    _assert_root()
    if stage not in ("BEGIN", "CONFIRM_REMOTE_REMOVED", "COMPLETE"):
        raise AuthorityError("unknown remove checkpoint")
    fd = _store(REMOVE_STATES)
    try:
        active = _current(repo, runner, runner_dir)
        if active is None:
            raise AuthorityError("untracked legacy runner requires admin review")
        instance = active["instance"]
        name = _instance_name(repo, runner, runner_dir, instance)
        previous = _read_record(fd, name)
        if stage == "BEGIN":
            created = _cycle_record(CREATE_STATES, repo, runner, runner_dir, instance)
            if created is None or created.get("stage") != "CREATE_COMPLETE":
                raise AuthorityError("creation not complete")
            if previous is not None:
                raise AuthorityError("prior remove attempt requires review")
            account = pwd.getpwnam(user)
            runnerfd = _safe_directory(runner_dir)
            try:
                st = os.fstat(runnerfd)
                if st.st_uid != account.pw_uid or st.st_mode & 0o022:
                    raise AuthorityError("invalid remove directory")
                path = os.stat(runner_dir, follow_symlinks=False)
                if (st.st_dev, st.st_ino) != (path.st_dev, path.st_ino):
                    raise AuthorityError("runner path changed")
                # The ordinary Remove identity gate is still independently
                # enforced by Worker and Dispatcher; record its pinned inode.
                metadata = os.open(".runner", os.O_RDONLY | os.O_NOFOLLOW |
                                   os.O_CLOEXEC, dir_fd=runnerfd)
                try:
                    reg = json.loads(os.read(metadata, 16385))
                    if (reg.get("agentName") != runner or
                            reg.get("gitHubUrl", "").rstrip("/").lower() !=
                            ("https://github.com/" + repo).lower()):
                        raise AuthorityError("runner identity mismatch")
                    remote_id = reg.get("agentId")
                    if remote_id is not None and not isinstance(remote_id, int):
                        raise AuthorityError("invalid runner ID")
                finally:
                    os.close(metadata)
            finally:
                os.close(runnerfd)
            if (created.get("directory_device"), created.get("directory_inode")) != (st.st_dev, st.st_ino):
                raise AuthorityError("create/remove inode mismatch")
            item = {"schema": 2, "instance": instance, "repository": repo, "runner": runner,
                    "runner_dir": runner_dir, "directory_device": st.st_dev,
                    "directory_inode": st.st_ino, "github_runner_id": remote_id,
                    "stage": "REMOTE_REMOVE_OUTCOME_UNKNOWN",
                    "created_at": int(time.time())}
            _publish(fd, name, item, replace=False)
            return {"device": st.st_dev, "inode": st.st_ino}
        if previous is None or (previous.get("repository"), previous.get("runner"),
                                 previous.get("runner_dir"), previous.get("schema")) != (
                                     repo, runner, runner_dir, 2) or previous.get("instance") != instance:
            raise AuthorityError("missing verified remove ledger")
        old = previous.get("stage")
        if stage == "CONFIRM_REMOTE_REMOVED":
            if old != "REMOTE_REMOVE_OUTCOME_UNKNOWN":
                raise AuthorityError("invalid remote removal sequence")
            runnerfd = _safe_directory(runner_dir)
            try:
                st = os.fstat(runnerfd)
                if (st.st_dev, st.st_ino) != (
                        previous["directory_device"], previous["directory_inode"]):
                    raise AuthorityError("removal target changed")
            finally:
                os.close(runnerfd)
            item = {**previous, "stage": "REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING",
                    "remote_confirmed_at": int(time.time())}
            _publish(fd, name, item, replace=True)
            return {"device": st.st_dev, "inode": st.st_ino}
        if old != "REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING":
            raise AuthorityError("local cleanup not pending")
        if os.path.lexists(runner_dir):
            raise AuthorityError("target directory still present")
        item = {**previous, "stage": "REMOVE_COMPLETE",
                "completed_at": int(time.time())}
        _publish(fd, name, item, replace=True)
        return {"complete": True}
    finally:
        os.close(fd)


def create_unit_attestation(repo: str, runner: str, runner_dir: str,
                            service: str, unit_path: str) -> None:
    """Record a Unit generated in a trusted 0644 root creation transaction.

    This historical proof is not permission to auto-repair a currently 0664
    Unit without separately demonstrated same-inode writer exclusion.
    """
    _assert_root()
    from grt_web_common import canonical_service_name
    if service != canonical_service_name(repo, runner):
        raise AuthorityError("Unit service scope mismatch")
    if unit_path != "/etc/systemd/system/" + service:
        raise AuthorityError("noncanonical Unit path")
    parent = _safe_directory(os.path.dirname(unit_path))
    try:
        fd = os.open(service, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                     os.O_CLOEXEC, dir_fd=parent)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_gid != 0
                    or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o644):
                raise AuthorityError("unsafe initial Unit")
            digest = hashlib.sha256()
            size = 0
            while True:
                raw = os.read(fd, 8192)
                if not raw:
                    break
                size += len(raw)
                if size > 16384:
                    raise AuthorityError("Unit too large")
                digest.update(raw)
            item = {"schema": 1, "issuer": "root-dispatcher-service-install",
                    "repository": repo, "runner": runner, "runner_dir": runner_dir,
                    "unit": service, "unit_path": unit_path,
                    "device": st.st_dev, "inode": st.st_ino, "sha256": digest.hexdigest(),
                    "mode": "0644", "issued_at": int(time.time())}
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    active = _current(repo, runner, runner_dir)
    if active is None:
        raise AuthorityError("Unit created without active cycle")
    item["instance"] = active["instance"]
    item["schema"] = 2
    storefd = _store(UNIT_ATTESTATIONS)
    try:
        name = _instance_name(repo, runner, runner_dir, active["instance"])
        if _read_record(storefd, name) is not None:
            raise AuthorityError("Unit provenance already exists")
        _publish(storefd, name, item, replace=False)
    finally:
        os.close(storefd)
