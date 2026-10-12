#!/usr/bin/env python3
"""Root-only, attempt-bound Web admin credential recovery record.

The journal is published before the group-readable auth file. An incomplete
attempt may replay only bytes recorded by this root-owned, exclusive file.
"""
import json
import os
import re
import stat
import sys

NAME = "web-auth-recovery.json"
FIELDS = {"schema", "attempt_id", "release", "payload_sha256", "auth"}
AUTH_RE = re.compile(r"PASSWORD_HASH=scrypt\$32768\$8\$1\$[A-Za-z0-9_-]{22}==\$[A-Za-z0-9_-]{43}=\n")

def fail():
    raise ValueError("invalid_web_auth_provenance")

def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail()
        result[key] = value
    return result

def secure_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (0, 0, 0o700):
        fail()
    return fd

def load(fd, attempt, release, sha):
    journal = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        st = os.fstat(journal)
        if not stat.S_ISREG(st.st_mode) or (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), st.st_nlink) != (0, 0, 0o600, 1) or st.st_size > 2048:
            fail()
        raw = os.read(journal, 2049)
    finally:
        os.close(journal)
    if len(raw) > 2048:
        fail()
    obj = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    if type(obj) is not dict or set(obj) != FIELDS or type(obj["schema"]) is not int or obj["schema"] != 1:
        fail()
    for key, expected in (("attempt_id", attempt), ("release", release), ("payload_sha256", sha)):
        if type(obj[key]) is not str or obj[key] != expected:
            fail()
    auth = obj["auth"]
    if type(auth) is not str or not AUTH_RE.fullmatch(auth):
        fail()
    return auth.encode("ascii")

def verify_auth(path, uid, gid, expected):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), st.st_nlink) != (uid, gid, 0o640, 1):
            fail()
        if st.st_size != len(expected) or os.read(fd, len(expected)+1) != expected:
            fail()
    finally:
        os.close(fd)

def main():
    if len(sys.argv) != 9:
        fail()
    mode, root, path, attempt, release, sha, uid_str, gid_str = sys.argv[1:]
    uid, gid = int(uid_str), int(gid_str)
    directory = secure_dir(root)
    try:
        if mode == "prepare":
            # No preexisting credential can be adopted as a new attempt.
            if os.path.lexists(path) or os.path.lexists(os.path.join(root, NAME)):
                fail()
            auth = sys.stdin.buffer.read(513)
            if len(auth) > 512 or not AUTH_RE.fullmatch(auth.decode("ascii")):
                fail()
            data = json.dumps({"schema":1, "attempt_id":attempt, "release":release,
                               "payload_sha256":sha, "auth":auth.decode("ascii")},
                              separators=(",", ":")).encode("ascii")
            fd = os.open(NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory)
        elif mode in ("verify", "publish"):
            expected = load(directory, attempt, release, sha)
            if os.path.lexists(path):
                verify_auth(path, 0, gid, expected)
            elif mode == "publish":
                # Exclusive creation; never overwrite an unknown credential.
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
                try:
                    os.fchown(fd, 0, gid)
                    os.write(fd, expected)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                verify_auth(path, 0, gid, expected)
            else:
                fail()
        else:
            fail()
    finally:
        os.close(directory)

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, UnicodeError, KeyError, json.JSONDecodeError):
        print("invalid_web_auth_provenance", file=sys.stderr)
        sys.exit(1)
