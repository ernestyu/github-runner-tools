#!/usr/bin/env python3
"""CLI-owned bounded operational JSONL logger. Never accepts free-text messages."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import sys
import time

ROOT = "/var/log/github-runner-tools"
MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
MAX_RECORD = 4096
RETENTION = 14 * 86400
OPS = {"create", "remove", "status"}
EVENTS = set("operation preflight archive_check release_lookup runner_download digest_check runner_extract dependency_check lifecycle_checkpoint remote_registration remote_removal registration_permissions registration_attestation service_install service_start service_health service_stop service_uninstall local_cleanup inventory operation_exit".split())
STAGES = set("none preflight PRE_REGISTRATION REGISTRATION_OUTCOME_UNKNOWN REGISTERED_PERMISSION_INCOMPLETE REGISTERED_UNIT_INCOMPLETE REGISTERED_START_INCOMPLETE REGISTERED_HEALTH_UNKNOWN CREATE_COMPLETE BEGIN CONFIRM_REMOTE_REMOVED COMPLETE REMOTE_REMOVE_OUTCOME_UNKNOWN REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING".split())
RESULTS = {"begin", "ok", "failed", "unknown", "skipped"}
REASONS = set("none invalid_input environment_unavailable archive_unavailable release_metadata_failed download_failed integrity_failed dependency_failed checkpoint_failed permissions_failed attestation_failed service_install_failed service_start_failed service_health_failed service_stop_failed service_uninstall_failed remote_result_uncertain local_cleanup_failed inventory_failed unexpected_cli_failure logging_unavailable".split())
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
RUNNER = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
ROTATED = re.compile(r"operational\.([0-9]{8}T[0-9]{12}Z)\.([0-9a-f]{32})\.jsonl\Z")
ACTIVE = "operational.jsonl"
LOCK = "operational.lock"
# The production location is fixed. Unit tests import Logger with an isolated directory.

class LoggingUnavailable(Exception):
    pass

def owned(st, mode, uid, gid=None):
    return (stat.S_ISREG(st.st_mode) and st.st_uid == uid and
            (gid is None or st.st_gid == gid) and stat.S_IMODE(st.st_mode) == mode and
            st.st_nlink == 1)

class Logger:
    def __init__(self, root=ROOT, uid=None):
        self.root = root
        self.uid = os.getuid() if uid is None else uid
        self.gid = os.getgid()
        self.df = None
        self.lf = None

    def __enter__(self):
        # Reject substituted directories and unexpected children; never create
        # production directory from an unprivileged CLI.
        if os.path.islink(self.root):
            raise LoggingUnavailable("unsafe_directory")
        if self.root == ROOT:
            # An untrusted ancestor could redirect the fixed log sink.
            walk = "/"
            for part in ROOT.strip("/").split("/")[:-1]:
                walk = os.path.join(walk, part)
                parent_st = os.lstat(walk)
                if (not stat.S_ISDIR(parent_st.st_mode) or parent_st.st_uid != 0
                        or parent_st.st_mode & 0o022):
                    raise LoggingUnavailable("unsafe_directory_parent")
        st = os.stat(self.root)
        if not stat.S_ISDIR(st.st_mode):
            raise LoggingUnavailable("unsafe_directory")
        if self.root == ROOT:
            actions = pwd.getpwnam("actions")
            if st.st_uid != 0 or st.st_gid != actions.pw_gid or stat.S_IMODE(st.st_mode) != 0o2770:
                raise LoggingUnavailable("unsafe_directory")
        try:
            self.df = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.lf = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.df)
            if not owned(os.fstat(self.lf), 0o600, self.uid):
                raise LoggingUnavailable("unsafe_lock")
            until = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(self.lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= until:
                        raise LoggingUnavailable("lock_timeout")
                    time.sleep(.02)
            self.maintain()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            self.df = None
            self.lf = None
            raise

    def __exit__(self, *_):
        if self.lf is not None:
            os.close(self.lf)
        if self.df is not None:
            os.close(self.df)

    def paths(self):
        active = None
        closed = []
        total = os.fstat(self.lf).st_size
        now = dt.datetime.now(dt.timezone.utc)
        for name in os.listdir(self.df):
            if name == LOCK:
                continue
            match = ROTATED.fullmatch(name)
            if name != ACTIVE and not match:
                raise LoggingUnavailable("unexpected_entry")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.df)
            try:
                st = os.fstat(fd)
                if not owned(st, 0o600, self.uid):
                    raise LoggingUnavailable("unsafe_log_file")
                # Detect partial and non-JSON trailing records before writing.
                if st.st_size:
                    # Last record must be complete JSONL; never silently truncate
                    # corrupted evidence or append behind an incomplete tail.
                    tail_len = min(st.st_size, MAX_RECORD + 1)
                    os.lseek(fd, -tail_len, os.SEEK_END)
                    tail = os.read(fd, tail_len)
                    if not tail.endswith(b"\n"):
                        raise LoggingUnavailable("corrupt_log_tail")
                    last = tail[:-1].split(b"\n")[-1]
                    if not last or len(last) + 1 > MAX_RECORD:
                        raise LoggingUnavailable("corrupt_log_tail")
                    try:
                        parsed = json.loads(last)
                    except (ValueError, UnicodeError) as exc:
                        raise LoggingUnavailable("corrupt_log_tail") from exc
                    if not isinstance(parsed, dict) or parsed.get("v") != 1:
                        raise LoggingUnavailable("corrupt_log_tail")
                total += st.st_size
                if name == ACTIVE:
                    active = st
                else:
                    ts = dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=dt.timezone.utc)
                    if ts > now + dt.timedelta(minutes=5):
                        raise LoggingUnavailable("invalid_rotation_time")
                    closed.append((ts, name, st.st_size))
            finally:
                os.close(fd)
        return active, sorted(closed), total

    def rotate(self, at=None):
        stamp = (at or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"operational.{stamp}.{secrets.token_hex(16)}.jsonl"
        os.rename(ACTIVE, name, src_dir_fd=self.df, dst_dir_fd=self.df)

    def maintain(self, required=0):
        active, closed, total = self.paths()
        now = dt.datetime.now(dt.timezone.utc)
        if active and active.st_size and (now.timestamp() - active.st_mtime > RETENTION or active.st_size >= MAX_FILE):
            expired = now.timestamp() - active.st_mtime > RETENTION
            self.rotate(dt.datetime.fromtimestamp(active.st_mtime, dt.timezone.utc) if expired else None)
            active, closed, total = self.paths()
        for ts, name, size in closed:
            if (now - ts).total_seconds() > RETENTION:
                os.unlink(name, dir_fd=self.df)
                total -= size
        active, closed, total = self.paths()
        for _ts, name, size in closed:
            if total + required <= MAX_TOTAL:
                break
            os.unlink(name, dir_fd=self.df)
            total -= size
        if total + required > MAX_TOTAL:
            raise LoggingUnavailable("capacity_exhausted")

    def append(self, record):
        raw = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        if len(raw) > MAX_RECORD:
            raise LoggingUnavailable("record_too_long")
        self.maintain(len(raw))
        active, _, _ = self.paths()
        if active and active.st_size + len(raw) > MAX_FILE:
            self.rotate()
            self.maintain(len(raw))
        fd = os.open(ACTIVE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.df)
        try:
            if not owned(os.fstat(fd), 0o600, self.uid):
                raise LoggingUnavailable("unsafe_active_log")
            if os.write(fd, raw) != len(raw):
                raise LoggingUnavailable("short_write")
        finally:
            os.close(fd)

def event(args):
    for value, allowed in [(args.op, OPS), (args.event, EVENTS), (args.stage, STAGES),
                           (args.result, RESULTS), (args.reason, REASONS)]:
        if value not in allowed:
            raise LoggingUnavailable("invalid_enum")
    if not RUN_ID.fullmatch(args.run_id):
        raise LoggingUnavailable("invalid_run_id")
    if args.repository != "none" and not REPO.fullmatch(args.repository):
        raise LoggingUnavailable("invalid_repository")
    if args.runner != "none" and not RUNNER.fullmatch(args.runner):
        raise LoggingUnavailable("invalid_runner")
    if not 0 <= args.seq <= 1000000 or not -1 <= args.exit_code <= 255:
        raise LoggingUnavailable("invalid_number")
    return dict(v=1, ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                run_id=args.run_id, seq=args.seq, op=args.op, event=args.event,
                stage=args.stage, result=args.result, reason=args.reason,
                exit_code=None if args.exit_code == -1 else args.exit_code,
                repository=None if args.repository == "none" else args.repository,
                runner=None if args.runner == "none" else args.runner)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--seq", type=int, required=True)
    p.add_argument("--op", required=True)
    p.add_argument("--event", required=True)
    p.add_argument("--stage", default="none")
    p.add_argument("--result", required=True)
    p.add_argument("--reason", default="none")
    p.add_argument("--exit-code", type=int, default=-1)
    p.add_argument("--repository", default="none")
    p.add_argument("--runner", default="none")
    a = p.parse_args()
    try:
        record = event(a)
        with Logger() as log:
            log.append(record)
    except (OSError, ValueError, LoggingUnavailable):
        print("CLI operational logging unavailable", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
